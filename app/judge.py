"""Typed judgments on each changed clause, from TypeSafe's Jev model (served by OpenCode).

Code owns the workflow: the diff engine finds what changed, Jev answers narrow questions about
it, and explicit policy below turns the answers into "tell the user" or "stay quiet".
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.gateway import Gateway
from app.metrics import dig
from app.watch import SectionChange

SYSTEMONE_PATH = "systemone/v1/systemone"
# Jev allows 32k tokens per question (state + question). Legal English runs ~0.25-0.3 tokens per
# character, so whole documents are capped at ~25k tokens and each side of a change at ~5k.
MAX_DOCUMENT_CHARS = 90_000
MAX_SECTION_CHARS = 20_000
CONTEXT_CHARS = 2_000  # unchanged text kept around the changed region

CATEGORIES: dict[str, str] = {
    "arbitration": "How disputes are resolved: arbitration, class-action waivers, jury waivers, governing law or venue.",
    "data": "What personal data is collected, how it is used, shared, sold, or used to train AI models.",
    "fees": "Prices, fees, charges, refunds, or billing terms.",
    "renewal": "Auto-renewal, trials converting to paid plans, cancellation steps or notice periods.",
    "liability": "Limits on the company's liability, warranties, indemnities you owe the company.",
    "content": "Rights the company takes over content you upload or create, licences you grant.",
    "termination": "When the company may suspend or close your account, or change the service.",
    "other": "None of the above, such as definitions, contact details, formatting, or wording-only edits.",
}

SEVERITY_LEVELS = [
    "The change only clarifies wording or fixes formatting; a user's rights and obligations stay the same.",
    "The change adjusts a minor detail that few users would notice or care about, such as a support contact or a notice address.",
    "The change alters what a typical user gives up or pays in a way they would want to know before continuing to use the service.",
    "The change removes a significant right, adds significant cost, or expands data use in a way many users would object to or act on.",
]

# Short names for SEVERITY_LEVELS, in the same order, for what members see.
SEVERITY_LABELS = ["Cosmetic", "Minor", "Worth knowing", "Major"]


def severity_label(score: float) -> str:
    # Halves round up (2.5 is Major); round() would send them to the even neighbour.
    return SEVERITY_LABELS[min(max(int(score + 0.5), 0), len(SEVERITY_LABELS) - 1)]


QUESTIONS: dict[str, dict[str, Any]] = {
    "material": {
        "type": "noul",
        "instructions": (
            "A company changed one section of its terms from `before` to `after`. Would an ordinary "
            "consumer who uses this service reasonably want to be told about this change, because it "
            "changes their rights, costs, obligations, or how their data is used?"
        ),
        "criteria": {
            "true": "The change affects the consumer's rights, money, obligations, or data in practice.",
            "false": "The change is cosmetic: wording, formatting, renumbering, links, or company contact details.",
        },
    },
    "category": {
        "type": "choice",
        "instructions": "Which area of the consumer relationship does the change from `before` to `after` affect most?",
        "criteria": CATEGORIES,
    },
    "opt_out": {
        "type": "noul",
        "instructions": (
            "Does the `after` text give the consumer a way to opt out of, reject, or object to this "
            "term, such as a deadline to send notice, an email address to write to, or a setting to change?"
        ),
    },
    "favours": {
        "type": "choice",
        "instructions": "Compared with `before`, who does the `after` text favour?",
        "criteria": {
            "company": "The company gains rights or the consumer loses them.",
            "consumer": "The consumer gains rights or protections.",
            "neutral": "Neither side gains; the change is balanced or descriptive.",
        },
    },
    "severity": {
        "type": "score",
        "instructions": "How much does the change from `before` to `after` matter to an ordinary consumer of this service?",
        "criteria": SEVERITY_LEVELS,
    },
}


Probability = Annotated[float, Field(ge=0, le=1)]


# --- System One response (OpenCode /systemone/v1/systemone), validated at the boundary ---------------------------------

class NoulAnswer(BaseModel):
    type: Literal["noul"]
    noul: Probability


class ChoiceAnswer(BaseModel):
    type: Literal["choice"]
    choice: str
    confidence: Probability
    probabilities: dict[str, Probability]


class ScoreAnswer(BaseModel):
    type: Literal["score"]
    score: float = Field(ge=0)
    confidence: Probability
    probabilities: dict[str, Probability]
    legend: dict[str, Any] = Field(default_factory=dict)


Answer = Annotated[NoulAnswer | ChoiceAnswer | ScoreAnswer, Field(discriminator="type")]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class SystemOneResponse(BaseModel):
    model: str
    answers: dict[str, Answer]
    usage: Usage = Field(default_factory=Usage)

    def noul(self, key: str) -> float:
        ans = self.answers[key]
        assert isinstance(ans, NoulAnswer), f"{key} is {ans.type}, expected noul"
        return ans.noul

    def choice(self, key: str) -> str:
        ans = self.answers[key]
        assert isinstance(ans, ChoiceAnswer), f"{key} is {ans.type}, expected choice"
        return ans.choice

    def score(self, key: str) -> float:
        ans = self.answers[key]
        assert isinstance(ans, ScoreAnswer), f"{key} is {ans.type}, expected score"
        return ans.score


Category = Literal["arbitration", "data", "fees", "renewal", "liability", "content", "termination", "other"]
Favours = Literal["company", "consumer", "neutral"]


class Verdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    # True when the change was too large to show the judge in full: report it regardless.
    partial: bool = False
    material: Probability
    category: Category
    opt_out: Probability
    favours: Favours
    severity: float = Field(ge=0, le=len(SEVERITY_LEVELS) - 1)

    def is_material(self, threshold: float | None = None) -> bool:
        if self.partial:  # fail closed: never stay quiet about a change the judge only partly saw
            return True
        return self.material >= (threshold if threshold is not None else settings.material_threshold)

    @property
    def needs_action(self) -> bool:
        return self.opt_out >= 0.7


def change_window(before: str, after: str) -> tuple[str, str, bool]:
    """The part of a long section that actually changed, with some context on either side.

    Returns (before, after, partial). Clipping from the start would hide a change that sits deep in
    a long section and make it look immaterial, so the window is centred on the differing region.
    `partial` is True when the differing region itself is too large to show in full; callers must
    then fail closed rather than trust a judgment made on cut-down text.
    """
    if len(before) <= MAX_SECTION_CHARS and len(after) <= MAX_SECTION_CHARS:
        return before, after, False
    prefix = 0
    limit = min(len(before), len(after))
    while prefix < limit and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and before[-1 - suffix] == after[-1 - suffix]:
        suffix += 1
    start = max(0, prefix - CONTEXT_CHARS)
    b_end = min(len(before), len(before) - suffix + CONTEXT_CHARS)
    a_end = min(len(after), len(after) - suffix + CONTEXT_CHARS)
    b, a = before[start:b_end], after[start:a_end]
    partial = len(b) > MAX_SECTION_CHARS or len(a) > MAX_SECTION_CHARS

    def frame(text: str, end: int, total: int) -> str:
        text = text[:MAX_SECTION_CHARS]
        return ("[…] " if start else "") + text + (" […]" if end < total or len(text) == MAX_SECTION_CHARS else "")

    return frame(b, b_end, len(before)) if before else "", frame(a, a_end, len(after)) if after else "", partial


def state_for(service: str, change: SectionChange) -> tuple[dict[str, Any], bool]:
    before, after, partial = change_window(change.before, change.after)
    state = {
        "service": service,
        "section": change.heading or "(untitled section)",
        "change_kind": change.kind,
        "before": before or "(this section did not exist)",
        "after": after or "(this section was removed)",
    }
    return state, partial


def parse_verdict(data: dict[str, Any], partial: bool = False) -> Verdict:
    resp = SystemOneResponse.model_validate(data)
    return Verdict(
        partial=partial,
        material=resp.noul("material"),
        category=resp.choice("category"),
        opt_out=resp.noul("opt_out"),
        favours=resp.choice("favours"),
        severity=resp.score("severity"),
    )


def _report_usage(gw: Gateway, data: Any) -> None:
    """What OpenCode says this judgment cost, and the tokens it counted."""
    gw.report("usd", dig(data, "usage", "cost"))
    gw.report("input_tokens", dig(data, "usage", "input_tokens"))
    gw.report("output_tokens", dig(data, "usage", "output_tokens"))


async def judge(gw: Gateway, service: str, change: SectionChange) -> Verdict:
    state, partial = state_for(service, change)
    body = {"model": settings.judge_model, "state": state, "questions": QUESTIONS}
    data = await gw.post(SYSTEMONE_PATH, json=body)
    _report_usage(gw, data)
    return parse_verdict(data, partial=partial)


# --- "Before you agree": judging a whole document you have not accepted yet ---------------------

RED_FLAGS: dict[str, str] = {
    "auto_renews": "Does the document say a subscription, membership or trial renews or converts to a paid plan automatically?",
    "arbitration": "Does the document require disputes to go to binding arbitration or waive class actions?",
    "data_sharing": "Does the document allow the company to share or sell the user's personal data to third parties for their own purposes, including advertising or AI training?",
    "unilateral_changes": "Does the document let the company change the terms or prices without the user's explicit agreement?",
    "content_licence": "Does the document give the company a broad licence to use, modify or sublicense content the user uploads?",
    "hard_to_cancel": "Does the document make cancelling hard, for example by requiring notice well in advance, a phone call, or a written letter?",
}


# What each red flag means, in plain English, for the 💡 buttons on verdict cards. Fixed text: showing
# one needs no provider call and no copy of the checked document. Each fits a 200-character pop-up.
FLAG_EXPLANATIONS: dict[str, str] = {
    "auto_renews": "Auto-renewal: the subscription or trial keeps going, and keeps charging you, unless you cancel "
                   "in time. Note the renewal date and how to cancel.",
    "arbitration": "Arbitration: disputes go to a private arbitrator instead of a court, and you usually can't join "
                   "a class action. Some terms let you opt out within a set time.",
    "data_sharing": "Data sharing: the company may pass your personal data to other companies for their own use, "
                    "such as advertising or training AI models.",
    "unilateral_changes": "One-sided changes: the company can change the terms or prices without asking you; "
                          "carrying on using the service counts as agreeing.",
    "content_licence": "Content licence: you let the company use, change and pass on what you upload, often for "
                       "free, worldwide and for as long as it likes.",
    "hard_to_cancel": "Hard to cancel: stopping takes extra steps, like notice well in advance, a phone call or a "
                      "letter, so you may keep paying longer than you want.",
}


async def red_flags(gw: Gateway, title: str, text: str) -> dict[str, float]:
    questions = {key: {"type": "noul", "instructions": q} for key, q in RED_FLAGS.items()}
    body = {"model": settings.judge_model, "state": {"document_title": title, "document": text}, "questions": questions}
    data = await gw.post(SYSTEMONE_PATH, json=body)
    _report_usage(gw, data)
    resp = SystemOneResponse.model_validate(data)
    return {key: resp.noul(key) for key in RED_FLAGS}


# --- "Ask about terms": how relevant is each candidate clause to a member's question ------------

RELEVANCE_CLAUSE_CHARS = 4_000


async def relevance(gw: Gateway, question: str, clauses: list[str]) -> list[float]:
    """One probability per clause, in order, that the clause helps answer the question."""
    if not clauses:
        return []
    state: dict[str, Any] = {"question": question}
    questions: dict[str, dict[str, Any]] = {}
    for n, clause in enumerate(clauses, start=1):
        state[f"clause_{n}"] = clause[:RELEVANCE_CLAUSE_CHARS]
        questions[f"c{n}"] = {
            "type": "noul",
            "instructions": f"Does `clause_{n}` contain information that helps answer `question`?",
        }
    body = {"model": settings.judge_model, "state": state, "questions": questions}
    data = await gw.post(SYSTEMONE_PATH, json=body)
    _report_usage(gw, data)
    resp = SystemOneResponse.model_validate(data)
    scores = []
    for n in range(1, len(clauses) + 1):
        ans = resp.answers.get(f"c{n}")
        scores.append(ans.noul if isinstance(ans, NoulAnswer) else 0.0)
    return scores

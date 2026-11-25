"""Typed judgments on each changed clause, from TypeSafe's Jev model.

Code owns the workflow: the diff engine finds what changed, Jev answers narrow questions about
it, and explicit policy below turns the answers into "tell the user" or "stay quiet".
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.gateway import Gateway
from app.watch import SectionChange

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


# --- TypeSafe /v1/systemone response, validated at the boundary ---------------------------------

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

    material: Probability
    category: Category
    opt_out: Probability
    favours: Favours
    severity: float = Field(ge=0, le=len(SEVERITY_LEVELS) - 1)

    def is_material(self, threshold: float | None = None) -> bool:
        return self.material >= (threshold if threshold is not None else settings.material_threshold)

    @property
    def needs_action(self) -> bool:
        return self.opt_out >= 0.7


def state_for(service: str, change: SectionChange) -> dict[str, Any]:
    return {
        "service": service,
        "section": change.heading or "(untitled section)",
        "change_kind": change.kind,
        "before": change.before or "(this section did not exist)",
        "after": change.after or "(this section was removed)",
    }


def parse_verdict(data: dict[str, Any]) -> Verdict:
    resp = SystemOneResponse.model_validate(data)
    return Verdict(
        material=resp.noul("material"),
        category=resp.choice("category"),
        opt_out=resp.noul("opt_out"),
        favours=resp.choice("favours"),
        severity=resp.score("severity"),
    )


async def judge(gw: Gateway, service: str, change: SectionChange) -> Verdict:
    body = {"model": settings.jev_model, "state": state_for(service, change), "questions": QUESTIONS}
    return parse_verdict(await gw.post("v1/systemone", json=body))


# --- "Before you agree": judging a whole document you have not accepted yet ---------------------

RED_FLAGS: dict[str, str] = {
    "auto_renews": "Does the document say a subscription, membership or trial renews or converts to a paid plan automatically?",
    "arbitration": "Does the document require disputes to go to binding arbitration or waive class actions?",
    "data_sharing": "Does the document allow the company to share or sell the user's personal data to third parties for their own purposes, including advertising or AI training?",
    "unilateral_changes": "Does the document let the company change the terms or prices without the user's explicit agreement?",
    "content_licence": "Does the document give the company a broad licence to use, modify or sublicense content the user uploads?",
    "hard_to_cancel": "Does the document make cancelling hard, for example by requiring notice well in advance, a phone call, or a written letter?",
}


async def red_flags(gw: Gateway, title: str, text: str) -> dict[str, float]:
    questions = {key: {"type": "noul", "instructions": q} for key, q in RED_FLAGS.items()}
    body = {"model": settings.jev_model, "state": {"document_title": title, "document": text}, "questions": questions}
    data = await gw.post(
        "v1/systemone", json=body, headers={"TypeSafe-Danger-Zone-Long-Context": "true"}
    )
    resp = SystemOneResponse.model_validate(data)
    return {key: resp.noul(key) for key in RED_FLAGS}

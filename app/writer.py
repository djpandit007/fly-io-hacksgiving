"""Plain-language writing, from a chat model on OpenCode (OpenAI-compatible chat completions)."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.config import settings
from app.gateway import Gateway

log = logging.getLogger(__name__)


CHAT_PATH = "openai/v1/chat/completions"


# Reasoning models think before answering, and the thinking counts against max_tokens. If the
# budget runs out mid-thought the reply has content=None; retry once with a bigger budget.
TOKEN_BUDGETS = (1500, 4000)


class EmptyReply(RuntimeError):
    pass


class _Message(BaseModel):
    role: str = "assistant"
    content: str | None = None


class _Choice(BaseModel):
    message: _Message
    finish_reason: str | None = None


class _CompletionDetails(BaseModel):
    reasoning_tokens: int | None = None


class _Usage(BaseModel):
    completion_tokens: int | None = None
    completion_tokens_details: _CompletionDetails | None = None


class ChatCompletion(BaseModel):
    choices: list[_Choice] = Field(default_factory=list)
    usage: _Usage | None = None

    def text(self) -> str:
        if not self.choices:
            return ""
        return (self.choices[0].message.content or "").strip()

    def why_empty(self) -> str:
        finish = self.choices[0].finish_reason if self.choices else "no choices"
        details = self.usage.completion_tokens_details if self.usage else None
        reasoning = details.reasoning_tokens if details else None
        return f"finish_reason={finish}, reasoning_tokens={reasoning}"

SYSTEM = (
    "You explain changes to consumer terms of service in plain English for one person who uses the "
    "service. Be concrete and brief. Never claim to give legal advice. Never invent facts that are "
    "not in the text you are given."
)


async def chat(gw: Gateway, prompt: str, system: str = SYSTEM) -> str:
    reason = ""
    for budget in TOKEN_BUDGETS:
        data = await gw.post(
            CHAT_PATH,
            json={
                "model": settings.writer_model,
                "max_tokens": budget,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            },
        )
        completion = ChatCompletion.model_validate(data)
        if text := completion.text():
            return text
        reason = completion.why_empty()
        log.warning("writer returned no text (max_tokens=%s, %s)", budget, reason)
    raise EmptyReply(f"the writing model returned no text ({reason})")


async def summarise_change(gw: Gateway, service: str, heading: str, before: str, after: str) -> str:
    prompt = (
        f"{service} changed the section '{heading}' of its terms.\n\nBEFORE:\n{before or '(new section)'}"
        f"\n\nAFTER:\n{after or '(section removed)'}\n\n"
        "In at most two sentences: what changed, and what it means for me. If I can opt out or act, "
        "say how and by when, using only what the text says."
    )
    return await chat(gw, prompt)


async def verdict_card(gw: Gateway, title: str, flags: dict[str, float], excerpt: str) -> str:
    raised = [k.replace("_", " ") for k, p in flags.items() if p >= 0.7]
    prompt = (
        f"I am about to agree to '{title}'. A checker flagged: {', '.join(raised) or 'nothing'}.\n\n"
        f"Document excerpt:\n{excerpt[:12000]}\n\n"
        "Reply with one line starting with SIGN, PUSH BACK or WALK AWAY, then up to three short "
        "bullets on the clauses that matter most to me."
    )
    return await chat(gw, prompt)


WRAPPED_SYSTEM = (
    "You write a short, witty year-in-review of the terms of service a person accepted, styled as a "
    "dissenting judicial opinion. Funny but accurate: only use the facts given. Under 200 words."
)


async def wrapped(gw: Gateway, facts: str) -> str:
    return await chat(gw, f"Facts for the year:\n{facts}", system=WRAPPED_SYSTEM)

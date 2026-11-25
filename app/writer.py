"""Plain-language writing, from a chat model on OpenCode (OpenAI-compatible chat completions)."""

from __future__ import annotations

from pydantic import BaseModel

from app.config import settings
from app.gateway import Gateway


CHAT_PATH = "openai/v1/chat/completions"


class _Message(BaseModel):
    role: str = "assistant"
    content: str


class _Choice(BaseModel):
    message: _Message


class ChatCompletion(BaseModel):
    choices: list[_Choice]

SYSTEM = (
    "You explain changes to consumer terms of service in plain English for one person who uses the "
    "service. Be concrete and brief. Never claim to give legal advice. Never invent facts that are "
    "not in the text you are given."
)


async def chat(gw: Gateway, prompt: str, system: str = SYSTEM) -> str:
    data = await gw.post(
        CHAT_PATH,
        json={
            "model": settings.writer_model,
            "max_tokens": 1500,  # reasoning models spend part of this before answering
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        },
    )
    completion = ChatCompletion.model_validate(data)
    if not completion.choices:
        raise ValueError("writer model returned no choices")
    return completion.choices[0].message.content.strip()


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

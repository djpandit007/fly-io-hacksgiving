"""A message on its way to a member: HTML text, optional inline buttons, and what to record if it is rated.

Email delivery uses only the text; Telegram delivery adds the buttons and, for rated answers, a 👍/👎 row.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.playbooks import ContractCheck
from app.telegram import Buttons

AnswerKind = Literal["verdict", "alert", "overview", "wrapped", "ask"]


class Answer(BaseModel):
    """What answer feedback stores about a rated answer: metadata only, never its text or recipient."""

    kind: AnswerKind
    model: str
    prompt_version: str
    change_id: int | None = None    # the public terms change an alert or /watch overview covers
    quotes_ok: bool | None = None   # verdicts: did every clause quote pass its check (None: none asked)


class Outgoing(BaseModel):
    text: str
    buttons: Buttons = Field(default_factory=list)
    rate: Answer | None = None
    # Verdict cards: the detected legal domain and the flags found (no document text), so a later
    # step can decide what to offer, e.g. a redline kit for a personal contract (`check.offers_redline`).
    check: ContractCheck | None = None


def as_outgoing(message: str | Outgoing) -> Outgoing:
    return message if isinstance(message, Outgoing) else Outgoing(text=message)

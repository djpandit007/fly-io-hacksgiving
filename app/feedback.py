"""Answer feedback: members rate the answers the bot writes, so the owner can see if they are valued.

Each rated answer gets a row found by an unguessable token carried in its 👍/👎 buttons. The row holds
metadata only: the kind of answer, the writer model and prompt version, the public terms change it
covers (alerts and /watch overviews), whether its clause quotes passed (verdicts), when it was sent, and
the vote. Never the answer text, a URL, or which member received or rated it: in a private chat only the
recipient can press the buttons, and the token stops forged taps from reaching other answers.

Recording must never change what the member receives, so `issue` swallows and logs its own errors (the
answer then goes out without rating buttons).
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Literal

from app import db, taps
from app.outgoing import Answer
from app.telegram import Buttons

log = logging.getLogger(__name__)

RETENTION_DAYS = 90
Vote = Literal["up", "down"]
REASON_LABELS = {"wrong": "Wrong", "unclear": "Unclear", "not_useful": "Not useful"}
KIND_LABELS = {
    "verdict": "Verdict cards", "alert": "Change alerts", "overview": "/watch overviews", "wrapped": "/wrapped",
    "ask": "/ask answers",
}


def issue(conn: sqlite3.Connection, answer: Answer) -> str | None:
    """Record a rated answer about to be sent and return its token, or None if it couldn't be recorded."""
    token = secrets.token_urlsafe(9)
    try:
        conn.execute(
            "INSERT INTO answers (token, kind, model, prompt_version, change_id, quotes_ok, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token, answer.kind, answer.model, answer.prompt_version, answer.change_id,
             None if answer.quotes_ok is None else int(answer.quotes_ok), db.now()),
        )
    except Exception as exc:  # noqa: BLE001 — the answer still goes out, just without rating buttons
        log.warning("answer feedback not recorded: %s", type(exc).__name__)
        return None
    return token


def rating_row(token: str) -> list[tuple[str, str]]:
    return [("👍", taps.build("fb", token, "u")), ("👎", taps.build("fb", token, "d"))]


def reason_row(token: str) -> list[tuple[str, str]]:
    return [(REASON_LABELS[why], taps.build("fr", token, code)) for code, why in taps.REASONS.items()]


def thanks_row() -> list[tuple[str, str]]:
    """After a vote: an inert button. Tapping it only says the rating is noted; nothing is recorded or edited."""
    return [("✓ Thanks", taps.build("k"))]


def with_rating(conn: sqlite3.Connection, buttons: Buttons, answer: Answer | None) -> Buttons:
    """The answer's buttons plus a 👍/👎 row in its own row, when it is rated and could be recorded."""
    if answer is None or (token := issue(conn, answer)) is None:
        return buttons
    return [*buttons, rating_row(token)]


def vote(conn: sqlite3.Connection, token: str, choice: Vote) -> bool:
    """One vote per answer; a later vote replaces the earlier one (and clears an old 👎 reason)."""
    return conn.execute(
        "UPDATE answers SET vote = ?, voted_at = ?, reason = CASE WHEN ? = 'down' THEN reason END "
        "WHERE token = ?",
        (choice, db.now(), choice, token),
    ).rowcount == 1


def reason(conn: sqlite3.Connection, token: str, why: str) -> bool:
    """Why a 👎: recorded only on an answer whose vote is 👎."""
    return conn.execute(
        "UPDATE answers SET reason = ? WHERE token = ? AND vote = 'down'", (why, token)
    ).rowcount == 1


def prune(conn: sqlite3.Connection, days: int = RETENTION_DAYS) -> None:
    try:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM answers WHERE sent_at < ?", (cutoff,))
    except Exception as exc:  # noqa: BLE001
        log.warning("pruning answer feedback failed: %s", type(exc).__name__)


def summary(conn: sqlite3.Connection, days: int) -> list[str]:
    """Lines for /stats: per kind of answer, ratings and 👍 share, 👎 reasons, and 👍 share per prompt version."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT kind, prompt_version, vote, reason, COUNT(*) AS n FROM answers "
        "WHERE vote IS NOT NULL AND voted_at >= ? GROUP BY kind, prompt_version, vote, reason",
        (since,),
    ).fetchall()
    if not rows:
        return []
    lines = []
    for kind, label in KIND_LABELS.items():
        mine = [r for r in rows if r["kind"] == kind]
        if not mine:
            continue
        total = sum(r["n"] for r in mine)
        up = sum(r["n"] for r in mine if r["vote"] == "up")
        reasons: dict[str, int] = {}
        for r in mine:
            if r["vote"] == "down" and r["reason"]:
                reasons[r["reason"]] = reasons.get(r["reason"], 0) + r["n"]
        why = ", ".join(f"{REASON_LABELS[k]} {n}" for k, n in sorted(reasons.items(), key=lambda kv: -kv[1]))
        versions: dict[str, list[int]] = {}
        for r in mine:
            v = versions.setdefault(r["prompt_version"], [0, 0])
            v[0] += r["n"]
            v[1] += r["n"] if r["vote"] == "up" else 0
        by_version = ", ".join(f"{name} {100 * u // n}% of {n}" for name, (n, u) in sorted(versions.items()))
        lines.append(
            f"• {label} — {total} rated, {100 * up // total}% 👍"
            + (f"; 👎 because: {why}" if why else "")
            + f" (by prompt: {by_version})"
        )
    return lines

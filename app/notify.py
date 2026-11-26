"""Deliver messages to members on their channel: Telegram when they have a chat, else one email digest."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import NamedTuple, Protocol

from app import feedback, mail, users
from app.agent import Clients, plain
from app.outgoing import Outgoing, as_outgoing
from app.telegram import Buttons

log = logging.getLogger(__name__)


class Sender(Protocol):
    configured: bool

    async def send(
        self, chat_id: int, text: str, *, buttons: Buttons | None = None, reply_to: int | None = None,
        markup: dict | None = None,
    ) -> list[int]: ...


class Delivered(NamedTuple):
    sent: int    # messages or email digests that went out
    failed: int  # members whose delivery raised part-way


async def deliver(
    conn: sqlite3.Connection, tg: Sender | None, cl: Clients, deliveries: dict[int, list[str | Outgoing]], subject: str
) -> Delivered:
    """Send each member their own messages (HTML). Reports what went out and how many members were missed.

    Telegram gets each message's buttons; an email digest uses the text only.
    """
    sent = failed = 0
    for user_id, messages in deliveries.items():
        user = users.active(users.by_id(conn, user_id))
        if not user or not messages:
            continue
        outgoing = [as_outgoing(m) for m in messages]
        try:
            if user.chat_id and tg is not None and tg.configured:
                for out in outgoing:
                    await send_outgoing(conn, tg, user.chat_id, out)
                    sent += 1
                    await asyncio.sleep(0.05)  # stay well inside Telegram's per-bot rate limit
            elif user.email and cl.mail is not None:
                body = "\n\n---\n\n".join(plain(o.text) for o in outgoing)
                await mail.send(cl.mail, user.email, subject, body + FOOTER)
                sent += 1
        except Exception as exc:  # noqa: BLE001 — one blocked chat or bounced address must not stop the others
            failed += 1
            log.warning("delivery to member %s failed: %s", user_id, exc)
    return Delivered(sent, failed)


async def send_outgoing(
    conn: sqlite3.Connection, tg: Sender, chat_id: int, out: Outgoing, reply_to: int | None = None
) -> list[int]:
    """One Outgoing to one Telegram chat, with its buttons and, for a rated answer, a 👍/👎 row."""
    buttons = feedback.with_rating(conn, out.buttons, out.rate)
    return await tg.send(chat_id, out.text, buttons=buttons or None, reply_to=reply_to)


FOOTER = "\n\n—\nFine Print · information, not legal advice. Reply to this address with a forwarded email or a document to check it."

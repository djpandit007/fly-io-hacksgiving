"""Deliver messages to members on their channel: Telegram when they have a chat, else one email digest."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Protocol

from app import mail, users
from app.agent import Clients, plain

log = logging.getLogger(__name__)


class Sender(Protocol):
    configured: bool

    async def send(self, chat_id: int, text: str) -> None: ...


async def deliver(
    conn: sqlite3.Connection, tg: Sender | None, cl: Clients, deliveries: dict[int, list[str]], subject: str
) -> int:
    """Send each member their own messages (HTML). Returns how many messages or digests went out."""
    sent = 0
    for user_id, messages in deliveries.items():
        user = users.active(users.by_id(conn, user_id))
        if not user or not messages:
            continue
        try:
            if user.chat_id and tg is not None and tg.configured:
                for text in messages:
                    await tg.send(user.chat_id, text)
                    sent += 1
                    await asyncio.sleep(0.05)  # stay well inside Telegram's per-bot rate limit
            elif user.email and cl.mail is not None:
                body = "\n\n---\n\n".join(plain(m) for m in messages)
                await mail.send(cl.mail, user.email, subject, body + FOOTER)
                sent += 1
        except Exception as exc:  # one blocked chat or bounced address must not stop the others
            log.warning("delivery to member %s failed: %s", user_id, exc)
    return sent


FOOTER = "\n\n—\nFine Print · information, not legal advice. Reply to this address with a forwarded email or a document to check it."

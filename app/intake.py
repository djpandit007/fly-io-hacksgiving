"""What happens to an email forwarded to the shared inbox (see design D7)."""

from __future__ import annotations

import logging
import re
import sqlite3
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app import agent, mail, users
from app.agent import Clients, plain
from app.config import settings
from app.db import User
from app.discover import find_terms_url, registrable_domain as _registrable
from app.notify import FOOTER, Sender, deliver
from app.telegram import esc

log = logging.getLogger(__name__)

UPDATE_SUBJECT = re.compile(r"(updat|chang|revis|new).{0,40}(terms|privacy|policy|agreement|conditions)", re.I)
URL = re.compile(r"https?://[^\s<>\"')\]]+")
TERMS_LINK = re.compile(r"(terms|tos\b|legal|agreement|conditions|privacy|policy)", re.I)
FORWARDED_FROM = re.compile(r"^\s*(?:>\s*)?From:\s*.*?<?([\w.+-]+@([\w-]+\.)+[\w-]+)>?\s*$", re.I | re.M)
MAILBOX_PROVIDERS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
                     "icloud.com", "me.com", "proton.me", "protonmail.com", "fastmail.com", "aol.com"}
PARSEABLE = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".docx", ".doc", ".html", ".htm", ".txt")
MIN_BODY_CHARS = 300
MAX_ATTACHMENTS = 3

HELP = (
    "Fine Print got your email but didn't find anything to check.\n\n"
    "Forward a \"we've updated our terms\" email, a receipt or a contract (as an attachment), or paste "
    "terms text, and I'll tell you what matters. Information, not legal advice."
)


def body_text(msg: mail.Message) -> str:
    if msg.text:
        return msg.text
    if msg.html:
        return BeautifulSoup(msg.html, "html.parser").get_text("\n", strip=True)
    return ""


def original_sender_domain(msg: mail.Message, text: str) -> str | None:
    """The company that sent the notice: the forwarded `From:` line, else the sender (if not a mailbox)."""
    for match in FORWARDED_FROM.finditer(text):
        domain = _registrable(match.group(1).rsplit("@", 1)[1])
        if domain not in MAILBOX_PROVIDERS and domain != _registrable(settings.agentmail_address.rsplit("@", 1)[-1] or "x"):
            return domain
    domain = _registrable(msg.sender.rsplit("@", 1)[-1]) if "@" in msg.sender else ""
    return domain if domain and domain not in MAILBOX_PROVIDERS else None


async def resolve_sender(conn: sqlite3.Connection, msg: mail.Message) -> User | None:
    """A verified member, or a new email-only member if the subject carries the join code. Else None."""
    if not msg.sender or msg.sender == settings.agentmail_address.lower():
        return None
    member = users.active(users.by_email(conn, msg.sender))
    if member:
        return member
    code = settings.join_code.get_secret_value()
    if code and code in (msg.subject or ""):
        return users.join_member(conn, code, email=msg.sender, name=msg.sender_name)
    return None


async def _attachments(conn: sqlite3.Connection, cl: Clients, user: User, msg: mail.Message) -> list[str]:
    cards = []
    for att in [a for a in msg.attachments if (a.filename or "").lower().endswith(PARSEABLE)][:MAX_ATTACHMENTS]:
        content = await mail.download_attachment(cl.mail, msg.message_id, att)
        if content is None:
            cards.append(f"<b>{esc(att.filename or 'attachment')}</b> is too large to read (8 MB limit).")
            continue
        cards.append(await agent.intake_file(conn, cl, user, att.filename or "document", content))
    return cards


async def _terms_update(conn: sqlite3.Connection, cl: Clients, tg: Sender | None, user: User, domain: str) -> list[str]:
    url = await find_terms_url(domain, cl)
    if not url:
        return [f"I couldn't find the terms page for {esc(domain)}. Send me the link and I'll watch it."]
    svc, past = await agent.subscribe(conn, cl, user, domain.split(".")[0].capitalize(), url)
    cards = [f"Now watching <b>{esc(svc.name)}</b> — {esc(svc.terms_url)}"]
    # Check right away; anything new goes to *every* watcher, not just the sender.
    fresh = await agent.check_service(conn, cl, svc)
    if fresh:
        fan_out: dict[int, list[str]] = {}
        vectors: dict[int, list[float]] = {}
        for change in fresh:
            for watcher in agent.subscribers(conn, svc.id):
                report = await agent.report_for(conn, cl, change, svc, watcher, vectors)
                (cards if watcher.id == user.id else fan_out.setdefault(watcher.id, [])).append(report.to_html())
        await deliver(conn, tg, cl, fan_out, f"Fine Print: {svc.name} changed its terms")
    else:
        cards += [r.to_html() for r in past[-3:]] or ["No material changes in the past year. I'll check daily."]
    return cards


async def process(conn: sqlite3.Connection, cl: Clients, tg: Sender | None, message_id: str) -> None:
    """Handle one received message end to end. Bodies and attachments are not stored."""
    if cl.mail is None:
        return
    msg = await mail.get_message(cl.mail, message_id)
    user = await resolve_sender(conn, msg)
    if user is None:
        log.info("ignoring mail from an unknown sender")
        return
    text = body_text(msg)[:200_000]
    subject = (msg.subject or "").strip()

    try:
        if any((a.filename or "").lower().endswith(PARSEABLE) for a in msg.attachments):
            cards = await _attachments(conn, cl, user, msg)
        elif UPDATE_SUBJECT.search(subject) and (domain := original_sender_domain(msg, text)):
            cards = await _terms_update(conn, cl, tg, user, domain)
        elif (link := next((u for u in URL.findall(text) if TERMS_LINK.search(urlparse(u).path)), None)):
            cards = [await agent.check_url(cl, link)]
        elif len(text) >= MIN_BODY_CHARS:
            cards = [await agent.verdict_card(cl, subject.removeprefix("Fwd: ").removeprefix("Fw: ") or "Forwarded email", text)]
        else:
            cards = []
    except Exception as exc:
        log.warning("processing mail failed: %s", exc)
        cards = ["Sorry, something went wrong while reading that email. Please try again later."]

    if user.chat_id and tg is not None and tg.configured:
        for card in cards or [esc(HELP)]:
            await tg.send(user.chat_id, card)
    reply_text = "\n\n---\n\n".join(plain(c) for c in cards) if cards else HELP
    await mail.reply(cl.mail, msg.message_id, reply_text + FOOTER)

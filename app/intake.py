"""What happens to an email forwarded to the shared inbox (see design D7)."""

from __future__ import annotations

import logging
import re
import sqlite3
from email.utils import parseaddr
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app import agent, mail, users
from app.agent import Clients, plain
from app.config import settings
from app.db import User
from app.discover import find_terms_url
from app.discover import registrable_domain as _registrable
from app.notify import FOOTER, Sender, deliver
from app.telegram import esc

log = logging.getLogger(__name__)

# Word stems, so "Policies", "Agreements" and "Conditions" match too.
UPDATE_SUBJECT = re.compile(r"(updat|chang|revis|new).{0,40}(term|privac|polic|agreement|condition)", re.IGNORECASE)
URL = re.compile(r"https?://[^\s<>\"')\]]+")
TERMS_LINK = re.compile(r"(terms|tos\b|legal|agreement|conditions|privacy|policy)", re.IGNORECASE)
MAX_HEADER_LINE = 300  # forwarded "From:" lines longer than this are ignored (no regex on long input)
MAILBOX_PROVIDERS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
                     "icloud.com", "me.com", "proton.me", "protonmail.com", "fastmail.com", "aol.com"}
PARSEABLE = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".docx", ".doc", ".html", ".htm", ".txt")
EMAIL_ADDRESS = users.EMAIL
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


def _forwarded_from_addresses(text: str):
    """Addresses on forwarded `From:` lines, parsed line by line (bounded work, no backtracking)."""
    for line in text.splitlines():
        if len(line) > MAX_HEADER_LINE:
            continue
        stripped = line.lstrip("> \t")
        if stripped[:5].lower() != "from:":
            continue
        address = parseaddr(stripped[5:].strip())[1].lower()
        if "@" in address and EMAIL_ADDRESS.match(address):
            yield address


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def original_sender_domain(msg: mail.Message, text: str) -> str | None:
    """The company that sent the notice.

    Forwards nest (your work address forwarded to your personal one, then to us), so collect every
    forwarded `From:` domain; prefer the one the subject names ("Updates to Discord's Policies"),
    else the deepest, which is the original sender. Fall back to the sender if it isn't a mailbox.
    """
    ours = _registrable(settings.agentmail_address.rsplit("@", 1)[-1]) if "@" in settings.agentmail_address else ""
    own = _registrable(msg.sender.rsplit("@", 1)[-1]) if "@" in msg.sender else ""
    candidates: list[str] = []
    for address in _forwarded_from_addresses(text):
        domain = _registrable(address.rsplit("@", 1)[1])
        if domain not in MAILBOX_PROVIDERS and domain not in (ours, own) and domain not in candidates:
            candidates.append(domain)
    subject = _letters(msg.subject or "")
    for domain in candidates:
        label = domain.split(".")[0]
        if len(label) >= 2 and _letters(label) in subject:
            return domain
    if candidates:
        return candidates[-1]
    return own if own and own not in MAILBOX_PROVIDERS and own != ours else None


CONFIRM_SUBJECT = "Confirm your email for Fine Print"
CONFIRM_TEXT = (
    "Someone (hopefully you) asked to use Fine Print with this address.\n\n"
    "To confirm, reply to this email and keep this code in your reply: {code}\n\n"
    "If it wasn't you, ignore this email and nothing happens."
)
CONFIRMED_TEXT = (
    "Confirmed — welcome to Fine Print. Forward \"we've updated our terms\" emails, receipts and contracts "
    "(as attachments) to this address and I'll reply with what matters. Information, not legal advice."
)


async def resolve_sender(conn: sqlite3.Connection, cl: Clients, msg: mail.Message, text: str) -> User | None:
    """The member this mail may act for, or None (after any join/confirmation handling).

    - A verified member address: that member.
    - A pending email-only address whose reply carries its code: confirm, acknowledge, act on nothing else.
    - The join code in the subject from an unknown address: start a confirmation (no member yet).
    - Anything else: ignored without a reply.
    The From line can be forged, so an address only becomes trusted after a reply proves the mailbox.
    """
    if not msg.sender or msg.sender == settings.agentmail_address.lower():
        return None
    member = users.active(users.by_email(conn, msg.sender))
    if member:
        return member
    confirmed = users.confirm_email_only(conn, msg.sender, f"{msg.subject or ''}\n{text[:5000]}")
    if confirmed is not None:
        if confirmed == "ok":
            await mail.respond(cl.mail, msg, CONFIRMED_TEXT + FOOTER)
        return None
    code = settings.join_code.get_secret_value()
    if code and code in (msg.subject or ""):
        try:
            issued = users.start_email_only_join(conn, code, msg.sender, msg.sender_name)
        except users.RateLimited as exc:
            log.info("email-only join rate-limited: %s", exc)
            return None
        if issued:
            await mail.send(cl.mail, msg.sender, CONFIRM_SUBJECT, CONFIRM_TEXT.format(code=issued) + FOOTER)
    return None


async def _attachments(conn: sqlite3.Connection, cl: Clients, user: User, msg: mail.Message) -> list[str]:
    cards = []
    for att in [a for a in msg.attachments if (a.filename or "").lower().endswith(PARSEABLE)][:MAX_ATTACHMENTS]:
        content = await mail.download_attachment(cl.mail, msg, att)
        if content is None:
            cards.append(
                f"I couldn't open <b>{esc(att.filename or 'the attachment')}</b> from email (too large or not "
                "reachable). Send it to the Fine Print bot in Telegram instead."
            )
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


async def process(
    conn: sqlite3.Connection, cl: Clients, tg: Sender | None, message_id: str, thread_id: str | None = None
) -> None:
    """Handle one received message end to end. Bodies and attachments are not stored."""
    if cl.mail is None:
        return
    msg = await mail.get_message(cl.mail, message_id, thread_id)
    if msg.suspicious or not msg.authenticated:
        log.info("ignoring mail that failed sender authentication or was flagged (%s)", msg.labels)
        return
    text = body_text(msg)[:200_000]
    user = await resolve_sender(conn, cl, msg, text)
    if user is None:
        return
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
    except Exception as exc:  # noqa: BLE001
        log.warning("processing mail failed: %s", exc)
        cards = ["Sorry, something went wrong while reading that email. Please try again later."]

    if user.chat_id and tg is not None and tg.configured:
        for card in cards or [esc(HELP)]:
            await tg.send(user.chat_id, card)
    reply_text = "\n\n---\n\n".join(plain(c) for c in cards) if cards else HELP
    await mail.respond(cl.mail, msg, reply_text + FOOTER)

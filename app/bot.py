"""Telegram conversation: the owner, members who joined with the join code, and nobody else.

Each member sees only their own watchlist, deadlines, calendar feed and Wrapped. Anything else
forwarded (a link, a PDF, a screenshot) gets a "before you agree" check.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from urllib.parse import urlparse

from app import agent, ledger, mail, metrics, users
from app.agent import Clients
from app.config import settings
from app.db import User
from app.discover import find_terms_url
from app.telegram import Incoming, Telegram, esc, redact

log = logging.getLogger(__name__)

URL = re.compile(r"https?://\S+")
# Counted by name in the owner's metrics; anything else a member types is counted as "other", never stored.
COMMANDS = frozenset(
    {"/start", "/help", "/watch", "/list", "/deadlines", "/email", "/verify", "/wrapped", "/members", "/remove", "/stats"}
)

HELP = """<b>Fine Print</b> watches the fine print you've agreed to.

/watch <i>url, domain or name</i> — watch a service's terms (shows the past year's changes)
/list — what you're watching, with community grades
/deadlines — your opt-out windows, renewals and notice periods, plus your calendar link
/wrapped — your year in accepted terms
{email}
Send a link or a file (PDF, screenshot, photo) to check it <b>before you agree</b>.
<i>Information, not legal advice.</i>"""

OWNER_HELP = """

<b>Owner</b>
/members — who has joined
/stats — how the bot is running: daily runs, providers, cost and usage
/remove <i>id</i> — remove a member"""

WELCOME = """Welcome to <b>Fine Print</b>, {name}! 👋

I read the terms you agree to and tell you when something that matters changes.
{forward}
Documents and links you send are read by AI providers on the operator's accounts; I keep only the
deadlines I extract and your watchlist. I give information, not legal advice."""

PRIVATE = "This is a private Fine Print bot: {hint}."


def _email_help() -> str:
    if not settings.agentmail_address:
        return ""
    return f"/email <i>address</i> — link your email, then forward terms emails to {esc(settings.agentmail_address)}\n"


def help_text(user: User) -> str:
    return HELP.format(email=_email_help()) + (OWNER_HELP if user.is_owner else "")


def _name_for(host: str) -> str:
    host = host.removeprefix("www.")
    return host.split(".")[-2].capitalize() if host.count(".") >= 1 else host


async def _start(conn: sqlite3.Connection, tg: Telegram, msg: Incoming, arg: str) -> User | None:
    """`/start <code>`: claim ownership or join. Returns the member it produced, if any."""
    owner = users.claim_owner(conn, msg.chat_id, arg, msg.name)
    if owner:
        await tg.send(msg.chat_id, "You're the owner of this Fine Print bot.\n\n" + help_text(owner))
        return owner
    member = users.join_member(conn, arg, chat_id=msg.chat_id, name=msg.name)
    if member:
        forward = (
            f"\nForward terms emails, receipts and contracts to <b>{esc(settings.agentmail_address)}</b> after /email.\n"
            if settings.agentmail_address else ""
        )
        await tg.send(msg.chat_id, WELCOME.format(name=esc(msg.name or "there"), forward=forward))
        await tg.send(msg.chat_id, help_text(member))
        return member
    hint = "ask the operator for a join code, then send /start followed by it"
    await tg.send(msg.chat_id, PRIVATE.format(hint=hint))
    return None


async def _watch(conn: sqlite3.Connection, cl: Clients, tg: Telegram, user: User, arg: str) -> None:
    chat = user.chat_id
    if not arg:
        await tg.send(chat, "Usage: /watch https://example.com/terms, /watch example.com or /watch notion")
        return
    url = arg if arg.startswith("http") else await find_terms_url(arg.strip("/ "), cl)
    if not url:
        await tg.send(chat, f"I couldn't find a terms page for {esc(arg)}. Send the full URL instead.")
        return
    name = _name_for(urlparse(url).hostname or arg)
    await tg.send(chat, f"Watching <b>{esc(name)}</b> — {esc(url)}\nLooking at the past year of changes…")
    svc, reports = await agent.subscribe(conn, cl, user, name, url)
    if not reports:
        await tg.send(chat, f"No material changes to {esc(svc.name)}'s terms in the past year. I'll check daily.")
        return
    for message in await agent.watch_messages(cl, svc, reports):
        await tg.send(chat, message)


async def _members(conn: sqlite3.Connection, tg: Telegram, user: User) -> None:
    lines = [
        f"{'👑' if m.is_owner else '•'} <code>{m.id}</code> {esc(m.label)}"
        f"{' · ' + esc(m.email) if m.email and m.chat_id else ''} · joined {m.joined_at:%d %b}"
        for m in users.members(conn)
    ]
    await tg.send(user.chat_id, "<b>Members</b>\n" + "\n".join(lines))


async def _remove(conn: sqlite3.Connection, tg: Telegram, user: User, arg: str) -> None:
    if not arg.isdigit():
        await tg.send(user.chat_id, "Usage: /remove <i>id</i> (see /members)")
        return
    ok = users.remove(conn, int(arg))
    await tg.send(user.chat_id, f"Removed member {arg}." if ok else f"No member {esc(arg)} to remove.")


VERIFY_REPLIES = {
    "ok": "✅ Linked {email}. Forward terms emails, receipts and contracts to {inbox} and I'll reply here and by email.",
    "wrong": "That code isn't right. Check the email and try /verify again.",
    "expired": "That code has expired. Send /email again for a new one.",
    "none": "There's no email waiting to be verified. Start with /email you@example.com",
    "too_many": "Too many wrong codes. Send /email again for a new one.",
    "in_use": "That address was just linked to another member, so I can't link it to you.",
    "locked": "Too many wrong codes. Email linking is paused for 24 hours.",
}


async def _email(conn: sqlite3.Connection, cl: Clients | None, tg: Telegram, user: User, arg: str) -> None:
    if cl is None or cl.mail is None or not settings.agentmail_address:
        await tg.send(user.chat_id, "Email isn't set up on this bot yet.")
        return
    try:
        code = users.start_email_link(conn, user, arg)
    except users.RateLimited as exc:
        await tg.send(user.chat_id, f"Not now: {esc(str(exc))}.")
        return
    except users.EmailInUse:
        await tg.send(user.chat_id, "That address is already linked to another member.")
        return
    except ValueError:
        await tg.send(user.chat_id, "Usage: /email you@example.com")
        return
    address = arg.strip().lower()
    await mail.send(
        cl.mail, address, "Your Fine Print code",
        f"Your Fine Print code is {code}.\n\nSend /verify {code} to the bot within 15 minutes to link this address. "
        "If you didn't ask for this, ignore this email.",
    )
    await tg.send(user.chat_id, f"I've emailed a 6-digit code to {esc(address)}. Send /verify <i>code</i> here.")


async def _verify(conn: sqlite3.Connection, tg: Telegram, user: User, arg: str) -> None:
    pending = users.by_id(conn, user.id)
    result = users.verify_email(conn, user, arg)
    text = VERIFY_REPLIES[result].format(
        email=esc(pending.email_pending or "") if pending else "", inbox=esc(settings.agentmail_address)
    )
    await tg.send(user.chat_id, text)


async def handle(conn: sqlite3.Connection, cl: Clients, tg: Telegram, msg: Incoming) -> None:
    cmd, _, arg = msg.text.strip().partition(" ")
    cmd, arg = cmd.lower().split("@")[0], arg.strip()

    user = users.active(users.by_chat(conn, msg.chat_id))
    if cmd == "/start" and (user is None or arg):
        if user is None:
            metrics.count(conn, "command", "/start")
            await _start(conn, tg, msg, arg)
            return
        # An existing member re-sending a code: maybe the owner claim; otherwise just show help.
        if arg and users.claim_owner(conn, msg.chat_id, arg, msg.name):
            user = users.by_chat(conn, msg.chat_id)
    if user is None:
        log.info("ignoring chat %s (not a member)", msg.chat_id)
        return
    users.set_name(conn, user, msg.name)
    chat = msg.chat_id
    if msg.file_id:
        used = "check:file"
    elif cmd in COMMANDS:
        used = cmd
    else:
        used = "check:url" if URL.search(msg.text) else "other"
    outcome = "ok"

    try:
        if msg.file_id:
            await tg.send(chat, "Reading it…")
            name, content = await tg.download(msg.file_id)
            await tg.send(chat, await agent.intake_file(conn, cl, user, msg.file_name or name, content))
            return

        if cmd in ("/start", "/help"):
            await tg.send(chat, help_text(user))
        elif cmd == "/watch":
            await _watch(conn, cl, tg, user, arg)
        elif cmd == "/list":
            lines = [
                f"• <b>{esc(s.name)}</b> {f'[{s.tosdr_grade}]' if s.tosdr_grade else ''} — {esc(s.terms_url)}"
                for s in agent.services_for(conn, user.id)
            ]
            await tg.send(chat, "\n".join(lines) or "Not watching anything yet. Try /watch youtube.com")
        elif cmd == "/deadlines":
            entries = ledger.upcoming(conn, user.id)
            lines = [f"⏰ <b>{e.due_on:%d %b %Y}</b>: {esc(e.title)}" for e in entries]
            feed = (
                f"\n\nYour private calendar feed (subscribe in any calendar app):\n"
                f"{esc(settings.public_url)}/calendar.ics?token={user.calendar_token}"
                if settings.public_url else ""
            )
            await tg.send(chat, ("\n".join(lines) or "No upcoming deadlines.") + feed)
        elif cmd == "/email":
            await _email(conn, cl, tg, user, arg)
        elif cmd == "/verify":
            await _verify(conn, tg, user, arg)
        elif cmd == "/wrapped":
            await tg.send(chat, await agent.wrapped(conn, cl, user))
        elif cmd in ("/members", "/remove", "/stats"):
            if not user.is_owner:
                await tg.send(chat, "That command is owner-only.")
            elif cmd == "/members":
                await _members(conn, tg, user)
            elif cmd == "/stats":
                await tg.send(chat, metrics.report(conn))
            else:
                await _remove(conn, tg, user, arg)
        elif m := URL.search(msg.text):
            await tg.send(chat, "Checking…")
            await tg.send(chat, await agent.check_url(cl, m.group(0)))
        else:
            await tg.send(chat, help_text(user))
    except Exception as exc:  # noqa: BLE001
        outcome = "error"
        log.error("handling message failed: %s", redact(f"{type(exc).__name__}: {exc}"))
        await tg.send(chat, f"Something went wrong: {esc(redact(str(exc))[:300])}")
    finally:
        metrics.count(conn, "command", used, outcome)

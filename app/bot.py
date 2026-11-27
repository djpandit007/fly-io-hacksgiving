"""Telegram conversation: the owner, members who joined with the join code, and nobody else.

Each member sees only their own watchlist, deadlines, calendar feed and Wrapped. Anything else
forwarded (a link, a PDF, a screenshot) gets a "before you agree" check.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from urllib.parse import urlparse

from app import (
    agent,
    ask,
    feedback,
    judge,
    ledger,
    mail,
    metrics,
    progress,
    taps,
    users,
)
from app.agent import Clients
from app.config import settings
from app.db import User
from app.discover import find_terms_url
from app.outgoing import Answer, Outgoing, as_outgoing
from app.telegram import Buttons, Incoming, Telegram, esc, redact

log = logging.getLogger(__name__)

URL = re.compile(r"https?://\S+")

# The / menu. One table for the menu and for the commands counted by name in the owner's metrics
# (anything else a member types is counted as "other", never stored).
MEMBER_MENU = [
    ("watch", "Watch a service's terms"),
    ("list", "What you're watching"),
    ("ask", "Ask about your terms"),
    ("deadlines", "Your opt-out windows, renewals and calendar link"),
    ("wrapped", "Your year in accepted terms"),
    ("help", "What I can do"),
]
EMAIL_MENU = [("email", "Link your email"), ("verify", "Confirm your email code")]
OWNER_MENU = [
    ("members", "Who has joined"),
    ("stats", "How the bot is running"),
    ("remove", "Remove a member"),
]
COMMANDS = frozenset({"/start", *(f"/{c}" for c, _ in MEMBER_MENU + EMAIL_MENU + OWNER_MENU)})

DESCRIPTION = (
    "Fine Print reads the terms of service you agree to and tells you when something that matters "
    "changes: arbitration, AI training on your data, auto-renewals. Send it a link or a contract to check "
    "before you agree.\n\nThis is a private bot. Ask the person who runs it for a join link."
)
SHORT_DESCRIPTION = "Watches the fine print you agree to. Private: ask the operator for a join link."

HELP_BUTTONS = [
    [("👀 Watch a service", taps.build("h", "watch")), ("📋 My list", taps.build("h", "list"))],
    [("⏰ Deadlines", taps.build("h", "dl")), ("🔎 How checks work", taps.build("h", "how"))],
]
HOW_CHECKS_WORK = (
    "<b>How checks work</b>\n\nSend me a link to terms, a PDF, a screenshot or a photo of a contract. I'll "
    "check it <b>before you agree</b>: red flags like auto-renewal, arbitration or data sharing, the clauses "
    "behind them, and any deadlines in it (added to your calendar)."
)
WATCH_PROMPT = "Which service should I watch? Reply with a name, a domain or a link to its terms."
WATCH_PLACEHOLDER = "e.g. notion or youtube.com"
POINTER = (
    "Not sure what you mean. Try one of these, or send me a link or a document to check. "
    "To ask about the terms you watch, use /ask."
)
# Single words that are conversation, not service names.
NOT_SERVICES = frozenset({"hi", "hello", "hey", "thanks", "thank", "thx", "ok", "okay", "yes", "no", "help", "start"})

HELP = """<b>Fine Print</b> watches the fine print you've agreed to.

/watch <i>url, domain or name</i> — watch a service's terms (shows the past year's changes)
/list — what you're watching, with community grades
/ask <i>question</i> — ask about the terms you watch, answered with quotes from them
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
Documents, links and questions you send are read by AI providers on the operator's accounts; I keep
only the deadlines I extract and your watchlist. I give information, not legal advice."""

PRIVATE = "This is a private Fine Print bot: {hint}."

ASK_USAGE = (
    "Ask me about the terms you watch, for example: /ask can Spotify raise my price?\n"
    "Your questions are read by the AI providers and not stored."
)
# Never the exception's text: it could carry the member's question.
ASK_FAILED = "Something went wrong answering that. Please try again."
ASK_RATED = frozenset({"answered", "quotes_only"})


def _email_help() -> str:
    if not settings.agentmail_address:
        return ""
    return f"/email <i>address</i> — link your email, then forward terms emails to {esc(settings.agentmail_address)}\n"


def help_text(user: User) -> str:
    return HELP.format(email=_email_help()) + (OWNER_HELP if user.is_owner else "")


def _name_for(host: str) -> str:
    host = host.removeprefix("www.")
    return host.split(".")[-2].capitalize() if host.count(".") >= 1 else host


def member_menu() -> list[tuple[str, str]]:
    return MEMBER_MENU + (EMAIL_MENU if settings.agentmail_address else [])


async def set_owner_menu(tg: Telegram, chat_id: int) -> None:
    """The owner's chat gets its own menu with the owner-only commands. Best-effort."""
    try:
        await tg.set_commands(member_menu() + OWNER_MENU, {"type": "chat", "chat_id": chat_id})
    except Exception as exc:  # noqa: BLE001 — a menu is a convenience; never fail a claim over it
        log.warning("setting the owner's command menu failed: %s", redact(str(exc)))


async def setup(conn: sqlite3.Connection, tg: Telegram) -> None:
    """Publish the description strangers see and the command menus. Best-effort, at startup."""
    steps = [
        ("description", tg.set_description(DESCRIPTION, SHORT_DESCRIPTION)),
        ("member menu", tg.set_commands(member_menu(), {"type": "all_private_chats"})),
    ]
    for what, call in steps:
        try:
            await call
        except Exception as exc:  # noqa: BLE001
            log.warning("bot setup (%s) failed: %s", what, redact(str(exc)))
    owner = users.owner(conn)
    if owner and owner.chat_id:
        await set_owner_menu(tg, owner.chat_id)


async def _send_help(tg: Telegram, user: User, prefix: str = "") -> None:
    await tg.send(user.chat_id, prefix + help_text(user), buttons=HELP_BUTTONS)


async def _ask_which_service(tg: Telegram, chat: int) -> None:
    await tg.send(chat, WATCH_PROMPT, markup={"force_reply": True, "input_field_placeholder": WATCH_PLACEHOLDER})


def _suggested(text: str) -> str | None:
    """A service name worth offering to watch, if the text is one (greetings are not)."""
    name = taps.suggestion(text)
    return name if name and name.lower() not in NOT_SERVICES else None


async def _suggest_or_point(tg: Telegram, chat: int, text: str) -> None:
    if name := _suggested(text):
        label = _name_for(name) if "." in name else name.capitalize()
        await tg.send(chat, f"Watch <b>{esc(label)}</b>?",
                      buttons=[[("Yes, watch it", taps.build("w", name)), ("No", taps.build("n"))]])
    else:
        await tg.send(chat, POINTER, buttons=HELP_BUTTONS)


async def _start(conn: sqlite3.Connection, tg: Telegram, msg: Incoming, arg: str) -> User | None:
    """`/start <code>`: claim ownership or join. Returns the member it produced, if any."""
    owner = users.claim_owner(conn, msg.chat_id, arg, msg.name)
    if owner:
        await set_owner_menu(tg, msg.chat_id)
        await _send_help(tg, owner, "You're the owner of this Fine Print bot.\n\n")
        return owner
    member = users.join_member(conn, arg, chat_id=msg.chat_id, name=msg.name)
    if member:
        forward = (
            f"\nForward terms emails, receipts and contracts to <b>{esc(settings.agentmail_address)}</b> after /email.\n"
            if settings.agentmail_address else ""
        )
        await tg.send(msg.chat_id, WELCOME.format(name=esc(msg.name or "there"), forward=forward))
        await _send_help(tg, member)
        return member
    hint = "ask the operator for a join link, or for a join code to send after /start"
    await tg.send(msg.chat_id, PRIVATE.format(hint=hint))
    return None


async def _watch(conn: sqlite3.Connection, cl: Clients, tg: Telegram, user: User, arg: str, reply_to: int | None) -> None:
    chat = user.chat_id
    if not arg:
        await _ask_which_service(tg, chat)
        return
    async with progress.working(tg, chat, reply_to) as w:
        w.stage("Finding the terms page…")
        url = arg if arg.startswith("http") else await find_terms_url(arg.strip("/ "), cl)
        buttons = None
        if not url:
            messages = [f"I couldn't find a terms page for {esc(arg)}. Send the full URL instead."]
        else:
            name = _name_for(urlparse(url).hostname or arg)
            svc, reports = await agent.subscribe(conn, cl, user, name, url, on_stage=w.stage)
            buttons = [[agent.unwatch_button(svc)]]
            watching = f"Watching <b>{esc(svc.name)}</b> — {esc(url)}\n{agent.documents_line(conn, svc)}"
            messages: list[str | Outgoing]
            if not reports:
                messages = [f"{watching}\nNo material changes to {esc(svc.name)}'s terms in the past year. I'll check daily."]
            else:
                w.stage("Writing the overview…")
                messages = list(await agent.watch_messages(cl, svc, reports))
                first = as_outgoing(messages[0])
                messages[0] = first.model_copy(update={"text": f"{watching}\n\n{first.text}"})
    await _reply(conn, tg, chat, messages, reply_to, buttons)


async def _reply(
    conn: sqlite3.Connection, tg: Telegram, chat: int, messages: list[str | Outgoing], reply_to: int | None,
    buttons: Buttons | None = None,
) -> None:
    """A finished answer: new messages (so the member is notified), the first threaded under theirs.

    Each message keeps its own buttons; `buttons` are added to the last one, and a rated message gets
    its 👍/👎 row below them.
    """
    outgoing = [as_outgoing(m) for m in messages]
    for i, out in enumerate(outgoing):
        rows = out.buttons + (buttons or []) if i == len(outgoing) - 1 else out.buttons
        rows = feedback.with_rating(conn, rows, out.rate)
        await tg.send(chat, out.text, reply_to=reply_to if i == 0 else None, buttons=rows or None)


def _watch_offer(name: str) -> Buttons | None:
    """The "Watch X?" suggestion's buttons for a service an answer named, or none if they can't carry it."""
    try:
        return [[("Yes, watch it", taps.build("w", name)), ("No", taps.build("n"))]]
    except ValueError:
        return None


async def _ask(conn: sqlite3.Connection, cl: Clients, tg: Telegram, user: User, question: str, reply_to: int | None) -> str:
    """Answer a question about the member's watched terms; returns the metric outcome."""
    chat = user.chat_id
    if not question:
        await tg.send(chat, ASK_USAGE)
        return "ok"
    async with progress.working(tg, chat, reply_to) as w:
        result = await ask.answer(conn, cl, user, question, on_stage=w.stage)
        w.failed = result.outcome == "error"  # answer() reports failures instead of raising
    rate = (
        Answer(kind="ask", model=settings.agent_model, prompt_version=ask.PROMPT_VERSION, quotes_ok=True)
        if result.outcome in ASK_RATED else None
    )
    buttons = _watch_offer(result.service) if result.outcome == "not_watching" else None
    await _reply(conn, tg, chat, [Outgoing(text=result.html, rate=rate)], reply_to, buttons)
    return "ok" if result.outcome == "answered" else result.outcome


async def _list(conn: sqlite3.Connection, tg: Telegram, user: User) -> None:
    services = agent.services_for(conn, user.id)
    lines = [
        f"• <b>{esc(s.name)}</b> {f'[{s.tosdr_grade}]' if s.tosdr_grade else ''} — {esc(s.terms_url)}"
        for s in services
    ]
    await tg.send(
        user.chat_id, "\n".join(lines) or "Not watching anything yet. Try /watch youtube.com",
        buttons=[[agent.unwatch_button(s)] for s in services],
    )


async def _deadlines(conn: sqlite3.Connection, tg: Telegram, user: User) -> None:
    entries = ledger.upcoming(conn, user.id)
    lines = [f"⏰ <b>{e.due_on:%d %b %Y}</b>: {esc(e.title)}" for e in entries]
    feed = (
        f"\n\nYour private calendar feed (subscribe in any calendar app):\n"
        f"{esc(settings.public_url)}/calendar.ics?token={user.calendar_token}"
        if settings.public_url else ""
    )
    await tg.send(user.chat_id, ("\n".join(lines) or "No upcoming deadlines.") + feed)


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


# --- Button taps ---------------------------------------------------------------------------------

PRIVATE_TAP = "This is a private Fine Print bot. Ask the operator for a join link."
STALE_TAP = "That button no longer works."


class _Tap:
    """One tap being handled: answers the callback exactly once (Telegram shows a spinner until then)."""

    def __init__(self, tg: Telegram, msg: Incoming) -> None:
        self.tg, self.msg = tg, msg
        self.answered = msg.callback_id is None  # a slow tap is answered before it queues

    async def answer(self, text: str = "", alert: bool = False) -> None:
        if self.answered:
            return
        self.answered = True
        try:
            await self.tg.answer_callback(self.msg.callback_id or "", text, alert)
        except Exception as exc:  # noqa: BLE001 — an expired callback must not fail the tap's work
            log.info("answering a tap failed: %s", type(exc).__name__)

    async def swap(self, new: tuple[str, str] | None) -> None:
        """Replace the tapped button (found by its data) with `new`, or remove it, keeping the other buttons.

        Telegram sends the message's current buttons with each tap; an old message may arrive without
        them, and then there is nothing to update.
        """
        await self.replace(lambda button: button[1] == self.msg.data, [new] if new else [])

    async def replace(self, match: Callable[[tuple[str, str]], bool], new: list[tuple[str, str]]) -> None:
        """Replace the matching buttons with `new` (placed where the first match was), keeping the rest."""
        rows: Buttons = []
        placed = False
        for row in self.msg.buttons:
            out: list[tuple[str, str]] = []
            for button in row:
                if not match(button):
                    out.append(button)
                elif not placed:
                    out.extend(new)
                    placed = True
            if out:
                rows.append(out)
        if self.msg.message_id and placed and rows != self.msg.buttons:  # Telegram refuses no-op edits
            try:
                await self.tg.edit_buttons(self.msg.chat_id, self.msg.message_id, rows)
            except Exception as exc:  # noqa: BLE001
                log.info("updating buttons failed: %s", type(exc).__name__)

    async def drop_buttons(self) -> None:
        """Remove the tapped message's buttons, unless it has none (already removed, or too old to tell)."""
        if self.msg.message_id and self.msg.buttons:
            try:
                await self.tg.edit_buttons(self.msg.chat_id, self.msg.message_id, None)
            except Exception as exc:  # noqa: BLE001
                log.info("removing buttons failed: %s", type(exc).__name__)


def _rating_of(token: str) -> Callable[[tuple[str, str]], bool]:
    """Matches an answer's rating buttons (👍/👎 or reasons) by its token."""
    return lambda button: button[1].startswith((f"v1:fb:{token}:", f"v1:fr:{token}:"))


async def handle_tap(conn: sqlite3.Connection, cl: Clients, tg: Telegram, msg: Incoming) -> None:
    t = _Tap(tg, msg)
    tap = taps.parse(msg.data)
    outcome = "ok"
    user = users.active(users.by_chat(conn, msg.chat_id))
    try:
        if user is None:
            await t.answer(PRIVATE_TAP, alert=True)
            return
        if tap is None:
            await t.answer(STALE_TAP)
            return
        await _dispatch(conn, cl, tg, t, user, tap)
    except Exception as exc:  # noqa: BLE001
        outcome = "error"
        log.error("handling tap failed: %s", redact(f"{type(exc).__name__}: {exc}"))
        await t.answer("Something went wrong. Please try again.")
        if tap and tap.action in taps.SLOW:  # its "On it…" toast is gone; say so in the chat
            await tg.send(msg.chat_id, f"Something went wrong: {esc(redact(str(exc))[:300])}")
    finally:
        await t.answer()
        if user is not None:
            name = taps.METRIC.get(tap.action) if tap else "unknown"
            if name:
                metrics.count(conn, "tap", name, outcome)


async def _dispatch(conn: sqlite3.Connection, cl: Clients, tg: Telegram, t: _Tap, user: User, tap: taps.Tap) -> None:
    match tap:
        case taps.Tap("h", "list"):
            await t.answer()
            await _list(conn, tg, user)
        case taps.Tap("h", "dl"):
            await t.answer()
            await _deadlines(conn, tg, user)
        case taps.Tap("h", "how"):
            await t.answer()
            await tg.send(user.chat_id, HOW_CHECKS_WORK)
        case taps.Tap("h", "watch"):
            await t.answer()
            await _ask_which_service(tg, user.chat_id)
        case taps.Tap("w", name):
            await t.answer("On it…")
            await t.drop_buttons()  # one tap, one /watch
            await _watch(conn, cl, tg, user, name, t.msg.message_id)
        case taps.Tap("n"):
            await t.drop_buttons()
            await t.answer("OK")
        case taps.Tap("fb", token, choice):
            vote: feedback.Vote = "up" if choice == "u" else "down"
            if not feedback.vote(conn, token, vote):
                await t.answer("This answer can't be rated any more.")
                return
            if vote == "up":
                await t.answer("Thanks!")
                await t.replace(_rating_of(token), feedback.thanks_row())
            else:
                await t.answer("Thanks. What was wrong? (optional)")
                await t.replace(_rating_of(token), feedback.reason_row(token))
        case taps.Tap("fr", token, code):
            if not feedback.reason(conn, token, taps.REASONS[code]):
                await t.answer("This answer can't be rated any more.")
                return
            await t.answer("Thanks, noted.")
            await t.replace(_rating_of(token), feedback.thanks_row())
        case taps.Tap("k"):
            await t.answer("Already noted. Thanks!")
        case taps.Tap("fx", flag):
            await t.answer(judge.FLAG_EXPLANATIONS[flag], alert=True)
        case taps.Tap("u", service_id):
            svc = agent.unsubscribe(conn, user, int(service_id))
            if svc is None:
                await t.answer("You're not watching that, so nothing changed.")
                return
            await t.answer(f"Stopped watching {svc.name}")
            await t.swap(agent.undo_button(svc))
        case taps.Tap("r", service_id):
            svc = agent.resubscribe(conn, user, int(service_id))
            if svc is None:
                await t.answer("That service can't be restored.")
                return
            await t.answer(f"Watching {svc.name} again")
            await t.swap(agent.unwatch_button(svc))
        case _:
            await t.answer(STALE_TAP)


async def handle(conn: sqlite3.Connection, cl: Clients, tg: Telegram, msg: Incoming) -> None:
    if msg.kind == "tap":
        await handle_tap(conn, cl, tg, msg)
        return
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
            await set_owner_menu(tg, msg.chat_id)
    if user is None:
        log.info("ignoring chat %s (not a member)", msg.chat_id)
        return
    users.set_name(conn, user, msg.name)
    chat = msg.chat_id
    if msg.reply_to_text == WATCH_PROMPT and msg.text.strip() and not msg.text.startswith("/") and not msg.file_id:
        cmd, arg = "/watch", msg.text.strip()
    question = ""
    if msg.file_id:
        used = "check:file"
    elif cmd in COMMANDS:
        used = cmd
        question = arg if cmd == "/ask" else ""
    elif URL.search(msg.text):
        used = "check:url"
    elif _suggested(msg.text) is None and ask.is_question(msg.text):
        used, question = "/ask", msg.text.strip()
    else:
        used = "other"
    outcome = "ok"

    try:
        if msg.file_id:
            async with progress.working(tg, chat, msg.message_id) as w:
                w.stage("Downloading…")
                name, content = await tg.download(msg.file_id)
                card = await agent.intake_file(conn, cl, user, msg.file_name or name, content, on_stage=w.stage)
            await _reply(conn, tg, chat, [card], msg.message_id)
            return

        if cmd in ("/start", "/help"):
            await _send_help(tg, user)
        elif cmd == "/watch":
            await _watch(conn, cl, tg, user, arg, msg.message_id)
        elif used == "/ask":
            outcome = await _ask(conn, cl, tg, user, question, msg.message_id)
        elif cmd == "/list":
            await _list(conn, tg, user)
        elif cmd == "/deadlines":
            await _deadlines(conn, tg, user)
        elif cmd == "/email":
            await _email(conn, cl, tg, user, arg)
        elif cmd == "/verify":
            await _verify(conn, tg, user, arg)
        elif cmd == "/wrapped":
            async with progress.working(tg, chat, msg.message_id) as w:
                w.stage("Looking back over your year…")
                text = await agent.wrapped(conn, cl, user)
            await _reply(conn, tg, chat, [text], msg.message_id)
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
            async with progress.working(tg, chat, msg.message_id) as w:
                card = await agent.check_url(cl, m.group(0), on_stage=w.stage)
            await _reply(conn, tg, chat, [card], msg.message_id)
        else:
            await _suggest_or_point(tg, chat, msg.text)
    except Exception as exc:  # noqa: BLE001
        outcome = "error"
        if used == "/ask":  # the exception's text could carry the question: log and show neither
            log.error("answering a question failed: %s", type(exc).__name__)
            await tg.send(chat, ASK_FAILED)
        else:
            log.error("handling message failed: %s", redact(f"{type(exc).__name__}: {exc}"))
            await tg.send(chat, f"Something went wrong: {esc(redact(str(exc))[:300])}")
    finally:
        metrics.count(conn, "command", used, outcome)

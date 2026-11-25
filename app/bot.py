"""Telegram conversation: one owner, a handful of commands, and anything forwarded gets checked."""

from __future__ import annotations

import logging
import re
import sqlite3
from urllib.parse import urlparse

from app import agent, ledger
from app.agent import Clients
from app.config import settings
from app.db import get_kv, set_kv
from app.discover import find_terms_url
from app.gmail import Gmail
from app.telegram import Incoming, Telegram, esc

log = logging.getLogger(__name__)

URL = re.compile(r"https?://\S+")
HELP = """<b>Fine Print</b> watches the fine print you've agreed to.

/watch <i>url or domain</i> — watch a service's terms (backfills a year of changes)
/list — what I'm watching, with community grades
/scan — find services from your inbox (read-only, senders and subjects only)
/approve <i>name</i> | all — start watching scanned services
/deadlines — opt-out windows, renewals and notice periods
/wrapped — your year in accepted terms

Send me a link or a file (PDF, screenshot, photo) to check it <b>before you agree</b>.
<i>Information, not legal advice.</i>"""


def owner(conn: sqlite3.Connection) -> int | None:
    value = get_kv(conn, "owner_chat")
    return int(value) if value else None


def _name_for(host: str) -> str:
    host = host.removeprefix("www.")
    return host.split(".")[-2].capitalize() if host.count(".") >= 1 else host


async def _watch(conn: sqlite3.Connection, cl: Clients, tg: Telegram, chat: int, arg: str) -> None:
    if not arg:
        await tg.send(chat, "Usage: /watch https://example.com/terms  or  /watch example.com")
        return
    url = arg if arg.startswith("http") else await find_terms_url(arg.strip("/ "))
    if not url:
        await tg.send(chat, f"I couldn't find a terms page on {esc(arg)}. Send the full URL instead.")
        return
    svc = agent.add_service(conn, _name_for(urlparse(url).hostname or arg), url, source="telegram")
    await tg.send(chat, f"Watching <b>{esc(svc.name)}</b> — {esc(url)}\nBackfilling a year of changes…")
    reports = await agent.onboard_service(conn, cl, svc)
    if not reports:
        await tg.send(chat, f"No material changes to {esc(svc.name)}'s terms in the past year. I'll check daily.")
    for r in reports:
        await tg.send(chat, r.to_html())


async def _scan(conn: sqlite3.Connection, tg: Telegram, chat: int) -> None:
    gmail = Gmail(settings.gmail_token_path)
    if not gmail.configured:
        await tg.send(chat, "Gmail isn't connected. Run scripts/gmail_auth.py on the Sprite first.")
        return
    try:
        await tg.send(chat, "Scanning your inbox for sign-ups and receipts…")
        watched = {s.domain for s in agent.services(conn)}
        proposed = []
        for stat in (await gmail.senders())[:15]:
            if stat.domain in watched:
                continue
            url = await find_terms_url(stat.domain)
            if url:
                agent.add_service(conn, _name_for(stat.domain), url, source="gmail", status="proposed")
                proposed.append(f"• <b>{esc(_name_for(stat.domain))}</b> ({stat.count} emails, e.g. “{esc(stat.sample_subject[:60])}”)")
        if proposed:
            await tg.send(chat, "Found these services:\n" + "\n".join(proposed) + "\n\n/approve all — or /approve <i>name</i>")
        else:
            await tg.send(chat, "No new services found.")
    finally:
        await gmail.aclose()


async def _approve(conn: sqlite3.Connection, cl: Clients, tg: Telegram, chat: int, arg: str) -> None:
    pending = agent.services(conn, status="proposed")
    chosen = pending if arg.lower() == "all" else [s for s in pending if s.name.lower() == arg.lower()]
    if not chosen:
        await tg.send(chat, "Nothing to approve. Try /scan first.")
        return
    total = 0
    for svc in chosen:
        conn.execute("UPDATE services SET status = 'active' WHERE id = ?", (svc.id,))
        reports = await agent.onboard_service(conn, cl, svc)
        total += len(reports)
        for r in reports[:3]:
            await tg.send(chat, r.to_html())
    await tg.send(chat, f"Now watching {len(chosen)} more service(s); found {total} material change(s) in the past year.")


async def handle(conn: sqlite3.Connection, cl: Clients, tg: Telegram, msg: Incoming) -> None:
    current = owner(conn)
    if current is None and msg.text.startswith("/start"):
        set_kv(conn, "owner_chat", str(msg.chat_id))
        current = msg.chat_id
    if msg.chat_id != current:
        log.info("ignoring chat %s (not the owner)", msg.chat_id)
        return

    chat = msg.chat_id
    try:
        if msg.file_id:
            await tg.send(chat, "Reading it…")
            name, content = await tg.download(msg.file_id)
            await tg.send(chat, await agent.intake_file(conn, cl, msg.file_name or name, content))
            return

        cmd, _, arg = msg.text.strip().partition(" ")
        cmd, arg = cmd.lower().split("@")[0], arg.strip()
        if cmd in ("/start", "/help"):
            await tg.send(chat, HELP)
        elif cmd == "/watch":
            await _watch(conn, cl, tg, chat, arg)
        elif cmd == "/list":
            svcs = agent.services(conn)
            lines = [f"• <b>{esc(s.name)}</b> {f'[{s.tosdr_grade}]' if s.tosdr_grade else ''} — {esc(s.terms_url)}" for s in svcs]
            await tg.send(chat, "\n".join(lines) or "Not watching anything yet. /watch or /scan.")
        elif cmd == "/scan":
            await _scan(conn, tg, chat)
        elif cmd == "/approve":
            await _approve(conn, cl, tg, chat, arg)
        elif cmd == "/deadlines":
            entries = ledger.upcoming(conn)
            lines = [f"⏰ <b>{e.due_on:%d %b %Y}</b>: {esc(e.title)}" for e in entries]
            cal = f"\n\nCalendar feed: {settings.public_url}/calendar.ics?token=…" if settings.public_url else ""
            await tg.send(chat, ("\n".join(lines) or "No upcoming deadlines.") + cal)
        elif cmd == "/wrapped":
            await tg.send(chat, await agent.wrapped(conn, cl))
        elif m := URL.search(msg.text):
            await tg.send(chat, "Checking…")
            await tg.send(chat, await agent.check_url(cl, m.group(0)))
        else:
            await tg.send(chat, HELP)
    except Exception as exc:
        log.exception("handling message failed")
        await tg.send(chat, f"Something went wrong: {esc(str(exc)[:300])}")

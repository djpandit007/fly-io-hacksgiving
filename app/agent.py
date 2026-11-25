"""The workflow. Code decides what happens; each provider supplies one kind of intelligence.

    fetch / backfill --> normalise --> diff vs snapshot --> Jev judgments per changed section
        --> (material) Ollama summary + Pinecone precedents + ledger deadline --> Telegram / Box
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app import box, judge, ledger, memory, parse, tosdr, writer
from app.config import settings
from app.db import Service, Snapshot, get_kv, now, one, rows, set_kv
from app.gateway import Gateway
from app.history import backfill
from app.judge import Verdict
from app.memory import Precedent
from app.telegram import esc
from app.watch import SectionChange, diff, fetch, html_to_markdown, markdown_to_canonical, normalise, opt_out_days, redline, sha

log = logging.getLogger(__name__)

MAX_SECTIONS_PER_VERSION = 20
MIN_HTML_CHARS = 400  # below this the page is probably rendered by JavaScript


class Clients:
    """Gateways for the providers that are configured; others are None and their step is skipped."""

    def __init__(self) -> None:
        def make(provider):
            return Gateway(provider) if provider.configured else None

        self.typesafe = make(settings.typesafe)
        self.ollama = make(settings.ollama)
        self.pinecone = make(settings.pinecone)
        self.box = make(settings.box)
        self.box_upload = make(settings.box_upload)
        # LlamaCloud has a default direct URL, so it counts as configured only with a key or a gateway URL.
        llama = settings.llama
        self.llama = Gateway(llama) if llama.key.get_secret_value() or "sprites.dev" in llama.base_url else None

    async def aclose(self) -> None:
        for gw in (self.typesafe, self.ollama, self.pinecone, self.box, self.box_upload, self.llama):
            if gw:
                await gw.aclose()


class ChangeReport(BaseModel):
    change_id: int
    service: str
    heading: str
    kind: str
    effective_at: datetime
    verdict: Verdict | None
    summary: str
    redline: str
    precedents: list[Precedent] = Field(default_factory=list)
    deadline: date | None = None

    def to_html(self) -> str:
        v = self.verdict
        badge = f"{v.category} · severity {v.severity:.1f}/3 · favours {v.favours}" if v else "unjudged"
        lines = [
            f"<b>{esc(self.service)}</b> changed <i>{esc(self.heading or 'its terms')}</i> ({self.effective_at:%d %b %Y})",
            f"<code>{esc(badge)}</code>",
            esc(self.summary),
        ]
        if self.deadline:
            lines.append(f"⏰ Opt-out window closes <b>{self.deadline:%a %d %b %Y}</b> (added to your calendar)")
        if self.precedents:
            others = ", ".join(sorted({p.service for p in self.precedents}))
            lines.append(f"📚 You already accepted similar wording from: {esc(others)}")
        return "\n".join(lines)


# --- Services ------------------------------------------------------------------------------------

def add_service(conn: sqlite3.Connection, name: str, url: str, source: str = "manual", status: str = "active") -> Service:
    domain = urlparse(url).hostname or ""
    conn.execute(
        """INSERT INTO services (name, terms_url, domain, source, status, added_at) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET terms_url = excluded.terms_url""",
        (name, url, domain.removeprefix("www."), source, status, now()),
    )
    svc = one(Service, conn.execute("SELECT * FROM services WHERE name = ?", (name,)))
    assert svc
    return svc


def services(conn: sqlite3.Connection, status: str = "active") -> list[Service]:
    return rows(Service, conn.execute("SELECT * FROM services WHERE status = ? ORDER BY name", (status,)))


# --- Core loop -----------------------------------------------------------------------------------

async def _judge(cl: Clients, service: str, change: SectionChange) -> Verdict | None:
    if not cl.typesafe:
        return None
    try:
        return await judge.judge(cl.typesafe, service, change)
    except Exception as exc:  # one bad section must not sink the run
        log.warning("judge failed for %s/%s: %s", service, change.heading, exc)
        return None


async def _summary(cl: Clients, service: str, change: SectionChange) -> str:
    if cl.ollama:
        try:
            return await writer.summarise_change(cl.ollama, service, change.heading, change.before, change.after)
        except Exception as exc:
            log.warning("summary failed: %s", exc)
    return redline(change.before, change.after)[:600]


async def record_version(
    conn: sqlite3.Connection, cl: Clients, svc: Service, text: str, taken_at: datetime, origin: str
) -> list[ChangeReport]:
    """Store a version of a service's terms; judge what changed since the previous one."""
    digest = sha(text)
    prev = one(
        Snapshot,
        conn.execute(
            "SELECT * FROM snapshots WHERE service_id = ? AND taken_at <= ? ORDER BY taken_at DESC LIMIT 1",
            (svc.id, taken_at.isoformat()),
        ),
    )
    if prev and prev.sha == digest:
        return []
    cur = conn.execute(
        "INSERT OR IGNORE INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (?, ?, ?, ?, ?)",
        (svc.id, taken_at.isoformat(), origin, digest, text),
    )
    if cur.rowcount == 0 or prev is None:  # duplicate text, or the first baseline
        return []
    if prev.origin == "ota" and origin != "ota":
        # Open Terms Archive extracts pages its own way; switching to our extraction would show
        # formatting differences as changes. The archive is updated within days, so re-baseline.
        log.info("%s: re-baselined on %s after archive history", svc.name, origin)
        return []
    snap_id = cur.lastrowid

    reports: list[ChangeReport] = []
    for change in diff(prev.text, text)[:MAX_SECTIONS_PER_VERSION]:
        verdict = await _judge(cl, svc.name, change)
        material = verdict.is_material() if verdict else True  # no judge configured: report everything
        change_id = conn.execute(
            """INSERT INTO changes (service_id, from_snap, to_snap, detected_at, effective_at, heading, kind,
                                    before_text, after_text, material, category, opt_out, severity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                svc.id, prev.id, snap_id, now(), taken_at.isoformat(), change.heading, change.kind,
                change.before, change.after,
                verdict.material if verdict else None, verdict.category if verdict else None,
                verdict.opt_out if verdict else None, verdict.severity if verdict else None,
            ),
        ).lastrowid
        if not material:
            continue

        summary = await _summary(cl, svc.name, change)
        conn.execute("UPDATE changes SET summary = ? WHERE id = ?", (summary, change_id))

        precedents: list[Precedent] = []
        if cl.pinecone and change.after:
            try:
                precedents = await memory.similar(cl.pinecone, change.after, exclude_service=svc.name, min_score=0.6)
                await memory.remember(
                    cl.pinecone, f"chg-{change_id}", svc.name, change.heading, change.after,
                    verdict.category if verdict else "other",
                )
            except Exception as exc:
                log.warning("pinecone failed: %s", exc)

        deadline = None
        if verdict and verdict.needs_action and (days := opt_out_days(change.after)):
            due = taken_at.date() + timedelta(days=days)
            if due >= date.today():
                deadline = due
                ledger.add(
                    conn, f"Opt out: {svc.name} — {change.heading or 'terms change'}", due, "opt_out",
                    detail=summary, service_id=svc.id, change_id=change_id,
                )

        reports.append(
            ChangeReport(
                change_id=change_id, service=svc.name, heading=change.heading, kind=change.kind,
                effective_at=taken_at, verdict=verdict, summary=summary,
                redline=redline(change.before, change.after)[:1500], precedents=precedents, deadline=deadline,
            )
        )
    return reports


async def page_text(cl: Clients, url: str) -> str:
    """Live terms page to normalised text; LlamaParse for PDFs and JavaScript-rendered pages."""
    content_type, body = await fetch(url)
    if "pdf" not in content_type:
        text = normalise(html_to_markdown(body))
        if len(text) >= MIN_HTML_CHARS or not cl.llama:
            return text
    if not cl.llama:
        raise RuntimeError(f"{url} is a PDF and LlamaParse is not configured")
    return normalise(markdown_to_canonical((await parse.parse_url(cl.llama, url)).markdown))


async def check_service(conn: sqlite3.Connection, cl: Clients, svc: Service) -> list[ChangeReport]:
    text = await page_text(cl, svc.terms_url)
    return await record_version(conn, cl, svc, text, datetime.now(UTC), "live")


async def onboard_service(conn: sqlite3.Connection, cl: Clients, svc: Service) -> list[ChangeReport]:
    """Backfill a year of history, then take the live version, then fetch the community grade."""
    reports: list[ChangeReport] = []
    for version in await backfill(svc.name, svc.terms_url):
        reports += await record_version(conn, cl, svc, version.text, version.taken_at, version.origin)
    try:
        reports += await check_service(conn, cl, svc)
    except Exception as exc:
        log.warning("live fetch failed for %s: %s", svc.name, exc)
    if rating := await tosdr.lookup(svc.domain or svc.name):
        conn.execute("UPDATE services SET tosdr_grade = ? WHERE id = ?", (rating.grade, svc.id))
    return reports


# --- Before you agree ----------------------------------------------------------------------------

async def verdict_card(cl: Clients, title: str, text: str, domain: str | None = None) -> str:
    flags: dict[str, float] = {}
    if cl.typesafe:
        flags = await judge.red_flags(cl.typesafe, title, text[:200_000])
    card = await writer.verdict_card(cl.ollama, title, flags, text) if cl.ollama else ""
    lines = [f"<b>{esc(title)}</b>"]
    if card:
        lines.append(esc(card))
    if flags:
        lines.append(
            "\n".join(f"{'🔴' if p >= 0.7 else '🟡' if p >= 0.3 else '🟢'} {k.replace('_', ' ')} ({p:.0%})" for k, p in flags.items())
        )
    if domain and (rating := await tosdr.lookup(domain)):
        grade = rating.grade or "unrated"
        lines.append(f"ToS;DR community grade: <b>{grade}</b> — <a href=\"{rating.url}\">details</a>")
    lines.append("<i>Information, not legal advice.</i>")
    return "\n\n".join(lines)


async def check_url(cl: Clients, url: str) -> str:
    text = await page_text(cl, url)
    host = (urlparse(url).hostname or url).removeprefix("www.")
    return await verdict_card(cl, f"Terms at {host}", text, host)


async def intake_file(conn: sqlite3.Connection, cl: Clients, filename: str, content: bytes) -> str:
    """A personal document: verdict card plus every dated obligation into the ledger."""
    if not cl.llama:
        return "LlamaParse is not configured, so I can't read files yet."
    parsed = await parse.parse_bytes(cl.llama, filename, content)
    added: list[str] = []
    try:
        doc = await parse.extract_deadlines(cl.llama, parsed.job_id)
        for d in doc.deadlines:
            if d.due and d.due >= date.today():
                cite = f" (page {d.page})" if d.page else ""
                if ledger.add(conn, f"{d.title} — {doc.document_title}", d.due, d.kind, detail=d.rule + cite):
                    added.append(f"⏰ {d.due:%d %b %Y}: {esc(d.title)}{cite}")
            elif d.rule:
                added.append(f"• {esc(d.title)}: {esc(d.rule)} (no fixed date)")
        title = doc.document_title
    except Exception as exc:
        log.warning("extract failed for %s: %s", filename, exc)
        title = filename
    card = await verdict_card(cl, title, parsed.markdown)
    if added:
        card += "\n\n<b>Deadlines</b>\n" + "\n".join(added)
    return card


async def process_box_inbox(conn: sqlite3.Connection, cl: Clients) -> list[str]:
    if not cl.box:
        return []
    seen = set(json.loads(get_kv(conn, "box_seen", "[]") or "[]"))
    messages = []
    for item in await box.list_inbox(cl.box):
        if item.id in seen or item.name.startswith("Fine Print report"):
            continue
        content = await box.download(cl.box, item.id)
        messages.append(f"📥 From Box: {esc(item.name)}\n\n" + await intake_file(conn, cl, item.name, content))
        seen.add(item.id)
        set_kv(conn, "box_seen", json.dumps(sorted(seen)))
    return messages


# --- Daily tick ----------------------------------------------------------------------------------

class TickResult(BaseModel):
    checked: int = 0
    failed: list[str] = Field(default_factory=list)
    reports: list[ChangeReport] = Field(default_factory=list)
    nudges: list[str] = Field(default_factory=list)
    inbox: list[str] = Field(default_factory=list)
    mail_hints: list[str] = Field(default_factory=list)


async def tick(conn: sqlite3.Connection, cl: Clients, gmail=None) -> TickResult:
    result = TickResult()
    for svc in services(conn):
        try:
            result.reports += await check_service(conn, cl, svc)
            result.checked += 1
        except Exception as exc:
            log.warning("check failed for %s: %s", svc.name, exc)
            result.failed.append(svc.name)

    if gmail and gmail.configured:
        try:
            watched = {s.domain for s in services(conn)}
            for mail in await gmail.terms_updates(days=2):
                if mail.domain not in watched:
                    result.mail_hints.append(f"{mail.domain}: “{mail.subject}” — not watched yet. /watch {mail.domain}")
        except Exception as exc:
            log.warning("gmail check failed: %s", exc)

    due = ledger.due_for_nudge(conn)
    result.nudges = [f"⏰ <b>{e.due_on:%a %d %b}</b>: {esc(e.title)}" for e in due]
    ledger.mark_nudged(conn, [e.id for e in due])

    try:
        result.inbox = await process_box_inbox(conn, cl)
    except Exception as exc:
        log.warning("box inbox failed: %s", exc)

    if result.reports and cl.box_upload:
        try:
            md = "\n\n".join(
                f"## {r.service}: {r.heading}\n\n{r.summary}\n\n```\n{r.redline}\n```" for r in result.reports
            )
            await box.upload_report(cl.box_upload, f"Fine Print report {date.today():%Y-%m-%d}.md", md)
        except Exception as exc:
            log.warning("box upload failed: %s", exc)
    return result


def tick_messages(result: TickResult) -> list[str]:
    msgs = [r.to_html() for r in result.reports]
    if result.nudges:
        msgs.append("<b>Coming up</b>\n" + "\n".join(result.nudges))
    msgs += result.inbox
    if result.mail_hints:
        msgs.append("<b>Terms-update emails</b>\n" + "\n".join(esc(h) for h in result.mail_hints))
    return msgs


# --- Agreed Wrapped ------------------------------------------------------------------------------

async def wrapped(conn: sqlite3.Connection, cl: Clients) -> str:
    since = (datetime.now(UTC) - timedelta(days=365)).isoformat()
    stats = conn.execute(
        """SELECT s.name AS service, c.category AS category, COUNT(*) AS n, MAX(c.severity) AS worst
           FROM changes c JOIN services s ON s.id = c.service_id
           WHERE c.effective_at >= ? AND c.material >= ?
           GROUP BY s.name, c.category ORDER BY n DESC""",
        (since, settings.material_threshold),
    ).fetchall()
    if not stats:
        return "Nothing material changed in the terms you accepted this year. Suspiciously quiet."
    facts = "\n".join(
        f"- {r['service']}: {r['n']} material change(s) about {r['category'] or 'other'}, worst severity {r['worst'] or 0:.1f}/3"
        for r in stats
    )
    total = sum(r["n"] for r in stats)
    body = await writer.wrapped(cl.ollama, facts) if cl.ollama else facts
    return f"<b>Agreed Wrapped {date.today():%Y}</b>\n{total} material changes you accepted by carrying on.\n\n{esc(body)}"

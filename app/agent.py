"""The workflow. Code decides what happens; each provider supplies one kind of intelligence.

    fetch / backfill --> normalise --> diff vs snapshot --> Jev judgments per changed section
        --> (material) plain-English summary, stored once per service
        --> per subscribing member: Pinecone precedents from *their* services + deadline in *their*
            ledger --> Telegram / email

Services, snapshots and changes are shared: each service is judged once however many members
watch it. Everything shown to a member is rendered for that member.
"""

from __future__ import annotations

import html
import logging
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from app import exa, judge, ledger, memory, parse, tosdr, writer
from app.config import settings
from app.db import Change, Service, Snapshot, User, now, one, rows
from app.gateway import Gateway
from app.history import backfill
from app.judge import Verdict
from app.locks import locks
from app.memory import Precedent
from app.netguard import BlockedURL
from app.telegram import esc, rich
from app.watch import (
    SectionChange,
    diff,
    fetch,
    html_to_markdown,
    markdown_to_canonical,
    normalise,
    opt_out_days,
    redline,
    sha,
)

log = logging.getLogger(__name__)

MAX_SECTIONS_PER_VERSION = 20
MIN_HTML_CHARS = 400  # below this the page is probably rendered by JavaScript


class Clients:
    """Gateways for the providers that are configured; others are None and their step is skipped."""

    def __init__(self) -> None:
        def make(provider):
            return Gateway(provider) if provider.configured else None

        # One OpenCode gateway serves both the judge (Jev) and the writer (chat model). It has a
        # default direct URL, so it counts as configured only with a key or a gateway URL.
        oc = settings.opencode
        self.ai = Gateway(oc) if oc.key.get_secret_value() or "sprites.dev" in oc.base_url else None
        self.pinecone = make(settings.pinecone)
        self.exa = make(settings.exa)
        self.mail = make(settings.agentmail) if settings.agentmail_inbox_id else None
        # LlamaCloud has a default direct URL, so it counts as configured only with a key or a gateway URL.
        llama = settings.llama
        self.llama = Gateway(llama) if llama.key.get_secret_value() or "sprites.dev" in llama.base_url else None

    async def aclose(self) -> None:
        for gw in (self.ai, self.pinecone, self.exa, self.mail, self.llama):
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
        if v and v.partial:
            badge += " · large change, partly judged: please skim it"
        lines = [
            f"<b>{esc(self.service)}</b> changed <i>{esc(self.heading or 'its terms')}</i> ({self.effective_at:%d %b %Y})",
            f"<code>{esc(badge)}</code>",
            rich(self.summary),
        ]
        if self.deadline:
            lines.append(f"⏰ Opt-out window closes <b>{self.deadline:%a %d %b %Y}</b> (added to your calendar)")
        if self.precedents:
            others = ", ".join(sorted({p.service for p in self.precedents}))
            lines.append(f"📚 You already accepted similar wording from: {esc(others)}")
        return "\n".join(lines)


# --- Services and subscriptions -------------------------------------------------------------------

def normalise_url(url: str) -> str:
    """Identity of a terms page: scheme-less, lower-case host without www., no query/fragment/trailing /."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower().removeprefix("www.")
    port = f":{u.port}" if u.port and u.port not in (80, 443) else ""
    return f"{host}{port}{u.path.rstrip('/')}"


def canonical_url(url: str) -> str:
    """What a shared service fetches: https, host and path only (no credentials, query or fragment).

    Storing the first watcher's raw URL would let them choose a different fetch target (scheme
    downgrade, credentials, query tricks) for everyone who later matches the same identity.
    """
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    port = f":{u.port}" if u.port and u.port not in (80, 443) else ""
    return f"https://{host}{port}{u.path.rstrip('/') or '/'}"


def find_service(conn: sqlite3.Connection, url: str) -> Service | None:
    key = normalise_url(url)
    for svc in rows(Service, conn.execute("SELECT * FROM services")):
        if normalise_url(svc.terms_url) == key:
            return svc
    return None


def add_service(conn: sqlite3.Connection, name: str, url: str) -> Service:
    """The shared service for this terms URL, created if new.

    Services are keyed by their (normalised) terms URL, and an existing service's URL is never
    changed by a later watcher: otherwise one member could repoint everyone's "YouTube" to a page they
    control (look-alike domain, or a user-generated page on a shared host) and push fake changes to all
    of its watchers. A display name already used by a different URL is disambiguated with the host.
    """
    url = canonical_url(url)
    existing = find_service(conn, url)
    if existing:
        return existing
    host = (urlparse(url).hostname or "").removeprefix("www.")
    display = name
    if one(Service, conn.execute("SELECT * FROM services WHERE name = ?", (display,))):
        display = f"{name} ({host})"
        n = 2
        while one(Service, conn.execute("SELECT * FROM services WHERE name = ?", (display,))):
            display, n = f"{name} ({host} #{n})", n + 1
    conn.execute(
        "INSERT INTO services (name, terms_url, domain, added_at) VALUES (?, ?, ?, ?)",
        (display, url, host, now()),
    )
    svc = one(Service, conn.execute("SELECT * FROM services WHERE name = ?", (display,)))
    assert svc
    return svc


def services_for(conn: sqlite3.Connection, user_id: int) -> list[Service]:
    """A member's watchlist."""
    return rows(
        Service,
        conn.execute(
            """SELECT s.* FROM services s JOIN subscriptions x ON x.service_id = s.id
               WHERE x.user_id = ? ORDER BY s.name""",
            (user_id,),
        ),
    )


def watched_services(conn: sqlite3.Connection) -> list[Service]:
    """Services that at least one active member watches: the daily check's work list."""
    return rows(
        Service,
        conn.execute(
            """SELECT DISTINCT s.* FROM services s
               JOIN subscriptions x ON x.service_id = s.id
               JOIN users u ON u.id = x.user_id AND u.status = 'active'
               ORDER BY s.name"""
        ),
    )


def subscribers(conn: sqlite3.Connection, service_id: int) -> list[User]:
    return rows(
        User,
        conn.execute(
            """SELECT u.* FROM users u JOIN subscriptions x ON x.user_id = u.id
               WHERE x.service_id = ? AND u.status = 'active' ORDER BY u.id""",
            (service_id,),
        ),
    )


# --- Core loop -----------------------------------------------------------------------------------

async def _judge(cl: Clients, service: str, change: SectionChange) -> Verdict | None:
    if not cl.ai:
        return None
    try:
        return await judge.judge(cl.ai, service, change)
    except Exception as exc:  # noqa: BLE001 — one bad section must not sink the run
        log.warning("judge failed for %s/%s: %s", service, change.heading, exc)
        return None


async def _summary(cl: Clients, service: str, change: SectionChange) -> str:
    if cl.ai:
        try:
            return await writer.summarise_change(cl.ai, service, change.heading, change.before, change.after)
        except Exception as exc:  # noqa: BLE001
            log.warning("summary failed: %s", exc)
    return redline(change.before, change.after)[:600]


def _is_material(change: Change) -> bool:
    if change.material is None:
        return True  # no judge configured: report everything
    return change.partial or change.material >= settings.material_threshold


def verdict_of(change: Change) -> Verdict | None:
    if change.material is None or change.category is None:
        return None
    return Verdict(
        partial=change.partial, material=change.material, category=change.category,
        opt_out=change.opt_out or 0.0, favours=change.favours or "neutral", severity=change.severity or 0.0,
    )


async def record_version(
    conn: sqlite3.Connection, cl: Clients, svc: Service, text: str, taken_at: datetime, origin: str
) -> list[Change]:
    """Store a version of a service's terms; judge what changed once; return the material changes."""
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

    material: list[Change] = []
    for change in diff(prev.text, text)[:MAX_SECTIONS_PER_VERSION]:
        verdict = await _judge(cl, svc.name, change)
        change_id = conn.execute(
            """INSERT INTO changes (service_id, from_snap, to_snap, detected_at, effective_at, heading, kind,
                                    before_text, after_text, material, category, opt_out, severity, favours, partial)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                svc.id, prev.id, snap_id, now(), taken_at.isoformat(), change.heading, change.kind,
                change.before, change.after,
                verdict.material if verdict else None, verdict.category if verdict else None,
                verdict.opt_out if verdict else None, verdict.severity if verdict else None,
                verdict.favours if verdict else None, int(verdict.partial) if verdict else 0,
            ),
        ).lastrowid
        if verdict and not verdict.is_material():
            continue
        summary = await _summary(cl, svc.name, change)
        conn.execute("UPDATE changes SET summary = ? WHERE id = ?", (summary, change_id))
        if cl.pinecone and change.after:
            try:
                await memory.remember(
                    cl.pinecone, f"chg-{change_id}", svc.name, change.heading, change.after,
                    verdict.category if verdict else "other",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("pinecone remember failed: %s", exc)
        stored = one(Change, conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,)))
        assert stored
        material.append(stored)
    return material


async def report_for(
    conn: sqlite3.Connection, cl: Clients, change: Change, svc: Service, user: User,
    vectors: dict[int, list[float]] | None = None,
) -> ChangeReport:
    """Render one stored change for one member: their precedents, their opt-out deadline."""
    verdict = verdict_of(change)
    precedents: list[Precedent] = []
    if cl.pinecone and change.after_text:
        try:
            names = [s.name for s in services_for(conn, user.id)]
            vector = None
            if vectors is not None and len(names) > 1:
                if change.id not in vectors:
                    vectors[change.id] = await memory.embed(cl.pinecone, change.after_text, "query")
                vector = vectors[change.id]
            precedents = await memory.similar(
                cl.pinecone, change.after_text, exclude_service=svc.name, services=names, min_score=0.45, vector=vector
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("pinecone similar failed: %s", exc)

    deadline = None
    if verdict and verdict.needs_action and (days := opt_out_days(change.after_text)):
        due = change.effective_at.date() + timedelta(days=days)
        if due >= date.today():
            deadline = due
            ledger.add(
                conn, user.id, f"Opt out: {svc.name} — {change.heading or 'terms change'}", due, "opt_out",
                detail=change.summary or "", service_id=svc.id, change_id=change.id,
            )
    return ChangeReport(
        change_id=change.id, service=svc.name, heading=change.heading, kind=change.kind,
        effective_at=change.effective_at, verdict=verdict,
        summary=change.summary or redline(change.before_text, change.after_text)[:600],
        redline=redline(change.before_text, change.after_text)[:1500], precedents=precedents, deadline=deadline,
    )


def past_material_changes(conn: sqlite3.Connection, service_id: int, days: int = 365) -> list[Change]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    found = rows(
        Change,
        conn.execute(
            "SELECT * FROM changes WHERE service_id = ? AND effective_at >= ? ORDER BY effective_at, id",
            (service_id, since),
        ),
    )
    return [c for c in found if _is_material(c)]


async def page_text(cl: Clients, url: str) -> str:
    """A terms page as normalised text.

    1. Direct fetch (public addresses only). 2. Exa page contents when the fetch is refused, fails or
    yields almost nothing (bot walls, JavaScript-only pages). 3. LlamaParse for PDFs and as last resort.
    A URL that resolves to a non-public address is never fetched and never handed to a fallback.
    """
    text, is_pdf, error = "", False, None
    try:
        content_type, body = await fetch(url)
        is_pdf = "pdf" in content_type
        if not is_pdf:
            text = normalise(html_to_markdown(body))
            if len(text) >= MIN_HTML_CHARS:
                return text
    except BlockedURL:
        raise
    except httpx.HTTPError as exc:
        error = exc
        log.info("direct fetch of %s failed (%s); trying fallbacks", url, type(exc).__name__)

    if not is_pdf and cl.exa:
        try:
            extracted = await exa.contents(cl.exa, url)
            candidate = normalise(markdown_to_canonical(extracted))
            if len(candidate) >= MIN_HTML_CHARS:
                return candidate
            text = max(text, candidate, key=len)
        except Exception as exc:  # noqa: BLE001
            log.warning("exa contents failed for %s: %s", url, exc)
    if cl.llama:
        return normalise(markdown_to_canonical((await parse.parse_url(cl.llama, url)).markdown))
    if text:
        return text
    raise RuntimeError(f"couldn't read {url}" + (f": {type(error).__name__}" if error else ""))


async def check_service(conn: sqlite3.Connection, cl: Clients, svc: Service) -> list[Change]:
    async with locks.hold(f"service:{svc.id}"):
        text = await page_text(cl, svc.terms_url)
        return await record_version(conn, cl, svc, text, datetime.now(UTC), "live")


def _has_history(conn: sqlite3.Connection, service_id: int) -> bool:
    return conn.execute("SELECT 1 FROM snapshots WHERE service_id = ? LIMIT 1", (service_id,)).fetchone() is not None


async def onboard_service(conn: sqlite3.Connection, cl: Clients, svc: Service) -> None:
    """Once per service: backfill a year of history, take the live version, fetch the community grade."""
    for version in await backfill(svc.name, svc.terms_url):
        await record_version(conn, cl, svc, version.text, version.taken_at, version.origin)
    try:
        await record_version(conn, cl, svc, await page_text(cl, svc.terms_url), datetime.now(UTC), "live")
    except Exception as exc:  # noqa: BLE001
        log.warning("live fetch failed for %s: %s", svc.name, exc)
    if rating := await tosdr.lookup(svc.domain or svc.name):
        conn.execute("UPDATE services SET tosdr_grade = ? WHERE id = ?", (rating.grade, svc.id))


async def subscribe(
    conn: sqlite3.Connection, cl: Clients, user: User, name: str, url: str
) -> tuple[Service, list[ChangeReport]]:
    """Add a service to a member's watchlist and show them its past year of material changes.

    The first watcher pays for the backfill and judging (under a per-service lock, so concurrent
    first watches do it once); later watchers get the stored results replayed with no judge calls.
    """
    async with locks.hold(f"service-url:{normalise_url(url)}"):
        svc = add_service(conn, name, url)
        if not _has_history(conn, svc.id):
            async with locks.hold(f"service:{svc.id}"):
                await onboard_service(conn, cl, svc)
        svc = one(Service, conn.execute("SELECT * FROM services WHERE id = ?", (svc.id,))) or svc
    conn.execute(
        "INSERT OR IGNORE INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)",
        (user.id, svc.id, now()),
    )
    vectors: dict[int, list[float]] = {}
    reports = [await report_for(conn, cl, c, svc, user, vectors) for c in past_material_changes(conn, svc.id)]
    return svc, reports


# --- Before you agree ----------------------------------------------------------------------------

async def verdict_card(cl: Clients, title: str, text: str, domain: str | None = None) -> str:
    flags: dict[str, float] = {}
    card = ""
    if cl.ai:
        flags = await judge.red_flags(cl.ai, title, text[: judge.MAX_DOCUMENT_CHARS])
        try:
            card = await writer.verdict_card(cl.ai, title, flags, text)
        except Exception as exc:  # noqa: BLE001 — the flags and grade are still worth sending
            log.warning("verdict card failed for %s: %s", title, exc)
    lines = [f"<b>{esc(title)}</b>"]
    if card:
        lines.append(rich(card))
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


async def intake_file(conn: sqlite3.Connection, cl: Clients, user: User, filename: str, content: bytes) -> str:
    """A personal document: verdict card plus every dated obligation into that member's ledger."""
    if not cl.llama:
        return "LlamaParse is not configured, so I can't read files yet."
    parsed = await parse.parse_bytes(cl.llama, filename, content)
    added: list[str] = []
    try:
        doc = await parse.extract_deadlines(cl.llama, parsed.job_id)
        for d in doc.deadlines:
            if d.due and d.due >= date.today():
                cite = f" (page {d.page})" if d.page else ""
                if ledger.add(conn, user.id, f"{d.title} — {doc.document_title}", d.due, d.kind, detail=d.rule + cite):
                    added.append(f"⏰ {d.due:%d %b %Y}: {esc(d.title)}{cite}")
            elif d.rule:
                added.append(f"• {esc(d.title)}: {esc(d.rule)} (no fixed date)")
        title = doc.document_title
    except Exception as exc:  # noqa: BLE001
        log.warning("extract failed for %s: %s", filename, exc)
        title = filename
    card = await verdict_card(cl, title, parsed.markdown)
    if added:
        card += "\n\n<b>Deadlines</b>\n" + "\n".join(added)
    return card


def plain(html_text: str) -> str:
    """Telegram HTML card to Markdown-ish plain text (for email)."""
    text = re.sub(r"</?b>", "**", html_text)
    text = re.sub(r"<a href=\"([^\"]+)\">([^<]*)</a>", r"[\2](\1)", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# --- Daily tick ----------------------------------------------------------------------------------

class TickResult(BaseModel):
    checked: int = 0
    failed: list[str] = Field(default_factory=list)
    changes: int = 0
    nudges: int = 0
    deliveries: dict[int, list[str]] = Field(default_factory=dict)  # user id -> messages (HTML)


async def tick(conn: sqlite3.Connection, cl: Clients) -> TickResult:
    """Check every watched service once and fan each material change out to its watchers."""
    result = TickResult()
    for svc in watched_services(conn):
        try:
            changes = await check_service(conn, cl, svc)
            result.checked += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("check failed for %s: %s", svc.name, exc)
            result.failed.append(svc.name)
            continue
        result.changes += len(changes)
        vectors: dict[int, list[float]] = {}
        for change in changes:
            for user in subscribers(conn, svc.id):
                report = await report_for(conn, cl, change, svc, user, vectors)
                result.deliveries.setdefault(user.id, []).append(report.to_html())

    due = ledger.due_for_nudge(conn)
    by_user: dict[int, list[str]] = {}
    for e in due:
        by_user.setdefault(e.user_id, []).append(f"⏰ <b>{e.due_on:%a %d %b}</b>: {esc(e.title)}")
    for user_id, lines in by_user.items():
        result.deliveries.setdefault(user_id, []).append("<b>Coming up</b>\n" + "\n".join(lines))
    result.nudges = len(due)
    ledger.mark_nudged(conn, [e.id for e in due])
    return result


# --- Agreed Wrapped ------------------------------------------------------------------------------

async def wrapped(conn: sqlite3.Connection, cl: Clients, user: User) -> str:
    """A member's year in accepted terms: material changes to the services *they* watch."""
    since = (datetime.now(UTC) - timedelta(days=365)).isoformat()
    stats = conn.execute(
        """SELECT s.name AS service, c.category AS category, COUNT(*) AS n, MAX(c.severity) AS worst
           FROM changes c JOIN services s ON s.id = c.service_id
           JOIN subscriptions x ON x.service_id = s.id AND x.user_id = ?
           WHERE c.effective_at >= ? AND (c.material >= ? OR c.partial = 1)
           GROUP BY s.name, c.category ORDER BY n DESC""",
        (user.id, since, settings.material_threshold),
    ).fetchall()
    if not stats:
        return "Nothing material changed in the terms you accepted this year. Suspiciously quiet."
    facts = "\n".join(
        f"- {r['service']}: {r['n']} material change(s) about {r['category'] or 'other'}, worst severity {r['worst'] or 0:.1f}/3"
        for r in stats
    )
    total = sum(r["n"] for r in stats)
    body = facts
    if cl.ai:
        try:
            body = await writer.wrapped(cl.ai, facts)
        except Exception as exc:  # noqa: BLE001
            log.warning("wrapped failed: %s", exc)
    return f"<b>Agreed Wrapped {date.today():%Y}</b>\n{total} material changes you accepted by carrying on.\n\n{rich(body)}"

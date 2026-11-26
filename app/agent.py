"""The workflow. Code decides what happens; each provider supplies one kind of intelligence.

    fetch / backfill --> normalise --> diff vs snapshot --> Jev judgments per changed section
        --> (material) plain-English summary, stored once per service
        --> per subscribing member: Pinecone precedents from *their* services + deadline in *their*
            ledger --> Telegram / email

Services, snapshots and changes are shared: each service is judged once however many members
watch it. Everything shown to a member is rendered for that member.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from app import exa, judge, ledger, memory, parse, quotes, taps, tosdr, writer
from app.config import settings
from app.db import Change, Service, Snapshot, User, now, one, rows
from app.gateway import Gateway, Recorder
from app.history import backfill
from app.judge import Verdict
from app.locks import locks
from app.memory import Precedent
from app.netguard import BlockedURL
from app.outgoing import Answer, AnswerKind, Outgoing
from app.progress import Stage, no_stage
from app.telegram import MAX_LEN, Buttons, esc, rich
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
AI_TIMEOUT = 180.0  # seconds per OpenCode request
OVERVIEW_TIMEOUT = 45.0  # a /watch reply never waits longer than this for its overview
# Overviews depend only on the changes (not the member), so each set is written once and shared.
# Keyed by the facts themselves: an edited summary or a deadline that has passed gives a new key.
OVERVIEW_CACHE: dict[tuple[int, str], str] = {}
OVERVIEW_CACHE_SIZE = 256


class Clients:
    """Gateways for the providers that are configured; others are None and their step is skipped."""

    def __init__(self, recorder: Recorder | None = None) -> None:
        def make(provider):
            return Gateway(provider, recorder=recorder) if provider.configured else None

        # One OpenCode gateway serves both the judge (Jev) and the writer (chat model). It has a
        # default direct URL, so it counts as configured only with a key or a gateway URL.
        oc = settings.opencode
        # Reasoning models can think for minutes on the larger writer budgets.
        self.ai = Gateway(oc, timeout=AI_TIMEOUT, recorder=recorder) if oc.key.get_secret_value() or "sprites.dev" in oc.base_url else None
        self.pinecone = make(settings.pinecone)
        self.exa = make(settings.exa)
        self.mail = make(settings.agentmail) if settings.agentmail_inbox_id else None
        # LlamaCloud has a default direct URL, so it counts as configured only with a key or a gateway URL.
        llama = settings.llama
        self.llama = Gateway(llama, recorder=recorder) if llama.key.get_secret_value() or "sprites.dev" in llama.base_url else None

    async def aclose(self) -> None:
        for gw in (self.ai, self.pinecone, self.exa, self.mail, self.llama):
            if gw:
                await gw.aclose()


FAVOURS_TEXT = {"company": "favours the company", "consumer": "favours you", "neutral": "neutral"}


FOUND, UNCLEAR = 0.7, 0.3  # how sure the judge must be to call a red flag found, or unclear


def flag_line(key: str, p: float) -> str:
    """One red flag in words; the number is how sure the judge is of the word shown."""
    name = key.replace("_", " ")
    if p >= FOUND:
        return f"🔴 Found ({p:.0%} sure): {name}"
    if p >= UNCLEAR:
        return f"🟡 Unclear ({p:.0%} likely): {name}"
    return f"🟢 Not found ({1 - p:.0%} sure): {name}"


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
    written: bool = True  # False when the summary is the raw redline (no writer, or the writer failed)

    def to_html(self) -> str:
        v = self.verdict
        badge = (
            f"{v.category} · {judge.severity_label(v.severity)} ({v.severity:.1f}/3) · {FAVOURS_TEXT[v.favours]}"
            if v else "unjudged"
        )
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
    fallback = redline(change.before_text, change.after_text)[:600]  # what _summary stores without a writer
    return ChangeReport(
        change_id=change.id, service=svc.name, heading=change.heading, kind=change.kind,
        effective_at=change.effective_at, verdict=verdict,
        summary=change.summary or fallback, written=bool(change.summary) and change.summary != fallback,
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
    conn: sqlite3.Connection, cl: Clients, user: User, name: str, url: str, on_stage: Stage = no_stage
) -> tuple[Service, list[ChangeReport]]:
    """Add a service to a member's watchlist and show them its past year of material changes.

    The first watcher pays for the backfill and judging (under a per-service lock, so concurrent
    first watches do it once); later watchers get the stored results replayed with no judge calls.
    """
    async with locks.hold(f"service-url:{normalise_url(url)}"):
        svc = add_service(conn, name, url)
        if not _has_history(conn, svc.id):
            on_stage("Looking up a year of past versions…")
            async with locks.hold(f"service:{svc.id}"):
                await onboard_service(conn, cl, svc)
        svc = one(Service, conn.execute("SELECT * FROM services WHERE id = ?", (svc.id,))) or svc
    conn.execute(
        "INSERT OR IGNORE INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)",
        (user.id, svc.id, now()),
    )
    on_stage("Collecting the changes that matter…")
    vectors: dict[int, list[float]] = {}
    reports = [await report_for(conn, cl, c, svc, user, vectors) for c in past_material_changes(conn, svc.id)]
    return svc, reports


def unsubscribe(conn: sqlite3.Connection, user: User, service_id: int) -> Service | None:
    """Take a service off this member's watchlist. None when they weren't watching it (nothing changes).

    The service's snapshots and judged changes stay; it drops out of the daily run once nobody watches it.
    """
    svc = one(Service, conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)))
    if svc is None:
        return None
    gone = conn.execute(
        "DELETE FROM subscriptions WHERE user_id = ? AND service_id = ?", (user.id, service_id)
    ).rowcount
    if not gone:
        return None
    conn.execute(
        "INSERT OR REPLACE INTO unwatched (user_id, service_id, unwatched_at) VALUES (?, ?, ?)",
        (user.id, service_id, now()),
    )
    return svc


def resubscribe(conn: sqlite3.Connection, user: User, service_id: int) -> Service | None:
    """Undo an unwatch: back on the watchlist, with no fetching, judging or replaying of past changes.

    Only a service this member unwatched can be restored. Undo buttons carry a bare service id, which a
    client can forge; without this check, trying ids would add (and so reveal) services other members watch.
    """
    svc = one(Service, conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)))
    if svc is None:
        return None
    if not conn.execute(
        "DELETE FROM unwatched WHERE user_id = ? AND service_id = ?", (user.id, service_id)
    ).rowcount:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)",
        (user.id, service_id, now()),
    )
    return svc


def unwatch_button(svc: Service) -> tuple[str, str]:
    return (f"🗑 Unwatch {svc.name}", taps.build("u", str(svc.id)))


def undo_button(svc: Service) -> tuple[str, str]:
    return (f"↩ Undo: watch {svc.name}", taps.build("r", str(svc.id)))


def rated(kind: AnswerKind, **meta) -> Answer:
    """What answer feedback records about a written answer: kind, writer model and prompt version."""
    return Answer(kind=kind, model=settings.writer_model, prompt_version=writer.PROMPT_VERSIONS[kind], **meta)


def alert(report: ChangeReport, svc: Service) -> Outgoing:
    """A material change as delivered to one watcher: the report, an unwatch button, and 👍/👎 when the
    writer wrote its summary (a raw redline isn't the writer's answer to rate)."""
    return Outgoing(text=report.to_html(), buttons=[[unwatch_button(svc)]],
                    rate=rated("alert", change_id=report.change_id) if report.written else None)


def pack(parts: list[str], limit: int = MAX_LEN, sep: str = "\n\n") -> list[str]:
    """Join parts into as few messages as fit within limit, never splitting a part.

    Telegram.send splits over-long text by characters, which can cut an HTML tag in half; packing at
    part boundaries avoids that. A part longer than limit gets a message of its own.
    """
    out: list[str] = []
    for part in parts:
        if out and len(out[-1]) + len(sep) + len(part) <= limit:
            out[-1] += sep + part
        else:
            out.append(part)
    return out


def _overview_fact(r: ChangeReport) -> str:
    v = r.verdict
    fields = [
        f"{r.effective_at:%d %b %Y}", r.heading or "terms", v.category if v else "unjudged",
        judge.severity_label(v.severity) if v else "—", FAVOURS_TEXT[v.favours] if v else "—", r.summary,
    ]
    line = "- " + " · ".join(" ".join(f.split()) for f in fields)
    return line + (f" · opt-out by {r.deadline:%d %b %Y}" if r.deadline else "")


async def overview(cl: Clients, svc: Service, reports: list[ChangeReport]) -> str:
    """A written roll-up of a service's past changes (Telegram HTML), or "" when it can't be had."""
    if not reports or not cl.ai:
        return ""
    facts = "\n".join(_overview_fact(r) for r in reports)
    if cached := OVERVIEW_CACHE.get((svc.id, facts)):
        return cached
    try:
        text = await asyncio.wait_for(writer.watch_overview(cl.ai, svc.name, facts), OVERVIEW_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — the cards are still worth sending
        log.warning("watch overview failed for %s: %s", svc.name, exc)
        return ""
    if len(OVERVIEW_CACHE) >= OVERVIEW_CACHE_SIZE:
        OVERVIEW_CACHE.clear()
    OVERVIEW_CACHE[(svc.id, facts)] = rich(text)
    return OVERVIEW_CACHE[(svc.id, facts)]


async def watch_messages(cl: Clients, svc: Service, reports: list[ChangeReport]) -> list[Outgoing]:
    """One /watch reply: header, overview, then every change card, packed into as few messages as fit.

    When an overview was written, the last message is rated (as the overview of the latest change)."""
    n = len(reports)
    header = f"<b>{esc(svc.name)}</b> — past year ({n} change{'s' if n != 1 else ''})"
    summary = await overview(cl, svc, reports)
    messages = [Outgoing(text=t) for t in pack([header, *([summary] if summary else []), *(r.to_html() for r in reports)])]
    if summary:
        latest = max(reports, key=lambda r: r.effective_at)
        messages[-1].rate = rated("overview", change_id=latest.change_id)
    return messages


# --- Before you agree ----------------------------------------------------------------------------

DISCLAIMER = "<i>Information, not legal advice.</i>"


def _explain_buttons(flags: dict[str, float]) -> Buttons:
    """A 💡 button per flag found or unclear, two to a row."""
    shown = [(f"💡 {k.replace('_', ' ')}", taps.build("fx", k)) for k, p in flags.items() if p >= UNCLEAR]
    return [shown[i : i + 2] for i in range(0, len(shown), 2)]


async def verdict_card(
    cl: Clients, title: str, text: str, domain: str | None = None, on_stage: Stage = no_stage
) -> Outgoing:
    """A "before you agree" card: verdict, the clauses behind found flags (verified against `text`),
    what was checked, the community grade, and a 💡 button per flag. Nothing of `text` is kept."""
    flags: dict[str, float] = {}
    card = ""
    passed: list[quotes.Quote] = []
    offered: list[quotes.Quote] = []
    if cl.ai:
        on_stage("Checking for red flags…")
        flags = await judge.red_flags(cl.ai, title, text[: judge.MAX_DOCUMENT_CHARS])
        on_stage("Writing it up…")
        try:
            raw = await writer.verdict_card(cl.ai, title, flags, text)
            card, offered = quotes.split_card(raw, {k for k, p in flags.items() if p >= FOUND})
            passed = quotes.verify(offered, text)
            if len(passed) < len(offered):
                log.info("dropped %d unverified clause quote(s) for %s", len(offered) - len(passed), title)
        except Exception as exc:  # noqa: BLE001 — the flags and grade are still worth sending
            log.warning("verdict card failed for %s: %s", title, exc)
    parts = [f"<b>{esc(title)}</b>"]
    if card:
        parts.append(rich(card))
    if passed:
        lines = "\n".join(f"<b>{esc(q.flag.replace('_', ' '))}</b>: “{esc(quotes.shown(q.text))}”" for q in passed)
        parts.append(f"<blockquote expandable><b>What the document says</b>\n{lines}</blockquote>")
    if flags:
        checked = "\n".join(flag_line(k, p) for k, p in flags.items())
        parts.append(f"<blockquote expandable><b>What I checked for</b>\n{checked}</blockquote>")
    if domain and (rating := await tosdr.lookup(domain)):
        grade = rating.grade or "unrated"
        parts.append(f"ToS;DR community grade: <b>{grade}</b> — <a href=\"{rating.url}\">details</a>")
    parts.append(DISCLAIMER)
    asked = any(p >= FOUND for p in flags.values())
    rate = rated("verdict", quotes_ok=(bool(offered) and len(passed) == len(offered)) if asked else None) if card else None
    return Outgoing(text="\n\n".join(parts), buttons=_explain_buttons(flags), rate=rate)


async def check_url(cl: Clients, url: str, on_stage: Stage = no_stage) -> Outgoing:
    on_stage("Fetching the page…")
    text = await page_text(cl, url)
    host = (urlparse(url).hostname or url).removeprefix("www.")
    return await verdict_card(cl, f"Terms at {host}", text, host, on_stage)


async def intake_file(
    conn: sqlite3.Connection, cl: Clients, user: User, filename: str, content: bytes, on_stage: Stage = no_stage
) -> Outgoing:
    """A personal document: verdict card plus every dated obligation into that member's ledger."""
    if not cl.llama:
        return Outgoing(text="LlamaParse is not configured, so I can't read files yet.")
    on_stage("Reading the document…")
    parsed = await parse.parse_bytes(cl.llama, filename, content)
    added: list[str] = []
    on_stage("Finding deadlines…")
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
    card = await verdict_card(cl, title, parsed.markdown, on_stage=on_stage)
    if added:
        deadlines = "\n\n<b>Deadlines</b>\n" + "\n".join(added)
        card = card.model_copy(update={"text": card.text.replace(DISCLAIMER, deadlines.lstrip() + "\n\n" + DISCLAIMER)})
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
    deliveries: dict[int, list[Outgoing]] = Field(default_factory=dict)  # user id -> messages (HTML + buttons)


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
                result.deliveries.setdefault(user.id, []).append(alert(report, svc))

    due = ledger.due_for_nudge(conn)
    by_user: dict[int, list[str]] = {}
    for e in due:
        by_user.setdefault(e.user_id, []).append(f"⏰ <b>{e.due_on:%a %d %b}</b>: {esc(e.title)}")
    for user_id, lines in by_user.items():
        result.deliveries.setdefault(user_id, []).append(Outgoing(text="<b>Coming up</b>\n" + "\n".join(lines)))
    result.nudges = len(due)
    ledger.mark_nudged(conn, [e.id for e in due])
    return result


# --- Agreed Wrapped ------------------------------------------------------------------------------

async def wrapped(conn: sqlite3.Connection, cl: Clients, user: User) -> Outgoing:
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
        return Outgoing(text="Nothing material changed in the terms you accepted this year. Suspiciously quiet.")
    facts = "\n".join(
        f"- {r['service']}: {r['n']} material change(s) about {r['category'] or 'other'}, worst severity {judge.severity_label(r['worst'] or 0)} ({r['worst'] or 0:.1f}/3)"
        for r in stats
    )
    total = sum(r["n"] for r in stats)
    body, rate = facts, None
    if cl.ai:
        try:
            body, rate = await writer.wrapped(cl.ai, facts), rated("wrapped")
        except Exception as exc:  # noqa: BLE001
            log.warning("wrapped failed: %s", exc)
    text = f"<b>Agreed Wrapped {date.today():%Y}</b>\n{total} material changes you accepted by carrying on.\n\n{rich(body)}"
    return Outgoing(text=text, rate=rate)

"""The web face of Fine Print on the Gateway Sprite's URL: a per-member Control Room.

Every page needs a signed-in member (magic link from `/dashboard` in Telegram, then a signed HTTP-only
cookie) and shows only that member's own data: nothing lists services, changes or contradictions for
services the member does not watch.

    GET /                    redirects to /room
    GET /auth/magic          exchanges a single-use magic link for a session cookie
    POST /auth/logout
    GET /room                the member's Terms Radar (their watched services by worst recent severity,
                             Policy Bundle coverage, ToS;DR grades, material changes, contradictions),
                             deadlines, jurisdiction, action receipts and Swarm Activity
    GET /changes/{id}        one change on a service the member watches: side-by-side redline, Jev scores,
                             benchmark, evidentiary screenshot (404 for any other change)
    GET /changes/{id}/evidence.png
    GET /api/swarm/stream    Server-Sent Events over sanitized `swarm_events` (signed-in members only)
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from app import agent, benchmark, ledger, sessions
from app.config import settings
from app.db import (
    DOC_ROLE_LABELS,
    ActionReceipt,
    Change,
    Contradiction,
    Service,
    ServiceDocument,
    User,
    one,
    rows,
)

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.autoescape = True  # also for any template not ending in .html

RECENT_DAYS = 90
RECENT_CHANGES = 20
CONTRADICTIONS = 10
SWARM_BACKLOG = 15

# Given the request (for app.state clients) and a change, return an object with `percentile` (0-100) and
# `cohort_size`, optionally `opt_out_share` (0-1), or None. Tests swap it out.
BenchmarkHook = Callable[[Request, Change], Awaitable[Any]]
BENCHMARK_TIMEOUT = 5.0


async def change_benchmark(request: Request, change: Change) -> Any:
    """The change's new clause against comparable public clauses (read-only Pinecone query)."""
    clients = getattr(request.app.state, "clients", None)
    gw = getattr(clients, "pinecone", None)
    if gw is None or not change.category or change.severity is None or not change.after_text:
        return None
    row = _conn(request).execute("SELECT name FROM services WHERE id = ?", (change.service_id,)).fetchone()
    return await benchmark.benchmark_clause(
        gw, change.after_text, change.category, change.severity, exclude_service=row["name"] if row else None,
    )


benchmark_hook: BenchmarkHook | None = change_benchmark

# SSE pacing; tests make these small.
SSE_POLL_SECONDS = 1.0
SSE_HEARTBEAT_POLLS = 15
SSE_BATCH = 50

BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
PRIVATE_HEADERS = BASE_HEADERS | {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _conn(request: Request) -> sqlite3.Connection:
    return request.app.state.conn


def _page(request: Request, name: str, context: dict, status: int = 200, headers: dict | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context, status_code=status, headers=headers or PRIVATE_HEADERS)


def _member(request: Request) -> User | None:
    return sessions.member(_conn(request), request.cookies.get(sessions.COOKIE))


def _marks(ids: list[int]) -> str:
    return ",".join("?" * len(ids))


# --- Member-scoped queries: each takes the ids of the services the member watches ----------------

def _material_sql(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    # Same rule as agent._is_material: unjudged changes count, partial verdicts count.
    return f"({p}material IS NULL OR {p}partial = 1 OR {p}material >= ?)"


def _radar(conn: sqlite3.Connection, services: list[Service], now: datetime) -> list[dict]:
    """The member's watched services ranked by their worst material change in the past RECENT_DAYS."""
    if not services:
        return []
    ids = [s.id for s in services]
    since = (now - timedelta(days=RECENT_DAYS)).isoformat()
    worst: dict[int, tuple[float | None, int]] = {}
    for r in conn.execute(
        f"""SELECT service_id, MAX(severity) AS worst, COUNT(*) AS n FROM changes
            WHERE service_id IN ({_marks(ids)}) AND effective_at >= ? AND {_material_sql()}
            GROUP BY service_id""",
        (*ids, since, settings.material_threshold),
    ):
        worst[r["service_id"]] = (r["worst"], r["n"])
    docs: dict[int, dict[str, ServiceDocument]] = {}
    for d in rows(ServiceDocument, conn.execute(
        f"SELECT * FROM service_documents WHERE service_id IN ({_marks(ids)})", ids
    )):
        docs.setdefault(d.service_id, {})[d.role] = d
    board = [
        {
            "service": s,
            "worst": worst.get(s.id, (None, 0))[0],
            "recent": worst.get(s.id, (None, 0))[1],
            "bundle": [(role, label, docs.get(s.id, {}).get(role)) for role, label in DOC_ROLE_LABELS.items()],
        }
        for s in services
    ]
    board.sort(key=lambda b: (b["worst"] is None, -(b["worst"] or 0), -b["recent"], b["service"].name.lower()))
    return board


def _recent_changes(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    found = conn.execute(
        f"""SELECT c.id, c.heading, c.kind, c.category, c.severity, c.material, c.summary, c.effective_at,
                   s.name AS service FROM changes c JOIN services s ON s.id = c.service_id
            WHERE c.service_id IN ({_marks(ids)}) AND {_material_sql("c")}
            ORDER BY c.effective_at DESC, c.id DESC LIMIT ?""",
        (*ids, settings.material_threshold, RECENT_CHANGES),
    ).fetchall()
    return [dict(r) for r in found]


def _contradictions(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    found = conn.execute(
        f"""SELECT x.*, s.name AS service, a.role AS role_a, b.role AS role_b
            FROM document_contradictions x JOIN services s ON s.id = x.service_id
            JOIN service_documents a ON a.id = x.doc_a_id JOIN service_documents b ON b.id = x.doc_b_id
            WHERE x.service_id IN ({_marks(ids)}) ORDER BY x.severity DESC, x.detected_at DESC LIMIT ?""",
        (*ids, CONTRADICTIONS),
    ).fetchall()
    out = []
    for r in found:
        d = dict(r)
        out.append({
            "c": Contradiction.model_validate({k: d[k] for k in Contradiction.model_fields}),
            "service": d["service"],
            "label_a": DOC_ROLE_LABELS.get(d["role_a"], d["role_a"]),
            "label_b": DOC_ROLE_LABELS.get(d["role_b"], d["role_b"]),
        })
    return out


_URLISH = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\S+@\S+")


def _sanitize(detail: str) -> str:
    """`detail` is meant to be a generic label; strip anything that looks like a URL or address anyway."""
    return _URLISH.sub("[redacted]", detail or "")[:80]


def _swarm_events(conn: sqlite3.Connection, after_id: int | None = None, limit: int = SWARM_BACKLOG) -> list[dict]:
    if after_id is None:  # the latest few, oldest first
        found = conn.execute(
            "SELECT * FROM (SELECT * FROM swarm_events ORDER BY id DESC LIMIT ?) ORDER BY id", (limit,)
        ).fetchall()
    else:
        found = conn.execute("SELECT * FROM swarm_events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
    return [
        {
            "id": r["id"], "role": r["sprite_role"], "event": r["event"], "detail": _sanitize(r["detail"]),
            "duration_ms": r["duration_ms"], "at": r["at"],
        }
        for r in found
    ]


# --- Redline -------------------------------------------------------------------------------------

_TOKENS = re.compile(r"\s+|[^\s]+")


def redline(before: str, after: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Word-level side-by-side diff: (segments for before, segments for after), each (kind, text) with
    kind "same", "del" (only in before) or "ins" (only in after). Templates escape the text."""
    a, b = _TOKENS.findall(before or ""), _TOKENS.findall(after or "")
    left: list[tuple[str, str]] = []
    right: list[tuple[str, str]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            left.append(("same", "".join(a[i1:i2])))
            right.append(("same", "".join(b[j1:j2])))
            continue
        if i2 > i1:
            left.append(("del", "".join(a[i1:i2])))
        if j2 > j1:
            right.append(("ins", "".join(b[j1:j2])))
    return left, right


# --- Change inspector: signed-in members, changes on their own watched services only ---------------

@router.get("/")
async def home() -> RedirectResponse:
    return RedirectResponse("/room", status_code=303, headers=PRIVATE_HEADERS)


def _watched_change(conn: sqlite3.Connection, user: User, change_id: int) -> tuple[Change, Service]:
    """The change, only if it belongs to a service this member watches. Anything else is a 404, so the
    ids of changes on other members' services cannot be probed."""
    change = one(Change, conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,)))
    watched = change is not None and conn.execute(
        "SELECT 1 FROM subscriptions WHERE user_id = ? AND service_id = ?", (user.id, change.service_id)
    ).fetchone() is not None
    svc = one(Service, conn.execute("SELECT * FROM services WHERE id = ?", (change.service_id,))) if watched else None
    if change is None or svc is None:
        raise HTTPException(status_code=404)
    return change, svc


def _evidence_file(conn: sqlite3.Connection, change: Change) -> Path | None:
    """The change's after-snapshot screenshot, only if it resolves to a file inside the evidence dir."""
    row = conn.execute("SELECT evidence_path FROM snapshots WHERE id = ?", (change.to_snap,)).fetchone()
    if not row or not row["evidence_path"]:
        return None
    base = settings.evidence_dir.resolve()
    try:
        path = (base / row["evidence_path"]).resolve()
    except (OSError, ValueError):
        return None
    if not path.is_relative_to(base) or path.suffix.lower() != ".png" or not path.is_file():
        return None
    return path


async def _benchmark(request: Request, change: Change) -> Any:
    if benchmark_hook is None:
        return None
    try:
        return await asyncio.wait_for(benchmark_hook(request, change), BENCHMARK_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — a missing badge must never break the page
        log.info("benchmark for change %s failed: %s", change.id, type(exc).__name__)
        return None


@router.get("/changes/{change_id}", response_class=HTMLResponse)
async def change_page(request: Request, change_id: int) -> HTMLResponse:
    conn = _conn(request)
    user = _member(request)
    if user is None:
        return _denied(request, SIGNED_OUT)
    change, svc = _watched_change(conn, user, change_id)
    doc = (
        one(ServiceDocument, conn.execute("SELECT * FROM service_documents WHERE id = ?", (change.document_id,)))
        if change.document_id else None
    )
    left, right = redline(change.before_text, change.after_text)
    return _page(request, "change.html", {
        "change": change,
        "service": svc,
        "document": doc.label if doc else "Terms",
        "left": left,
        "right": right,
        "scores": [
            ("Material", change.material, "How likely the change matters in practice"),
            ("Severity", change.severity, "How much it matters to an ordinary user"),
            ("Opt-out", change.opt_out, "How likely the new text offers a way to opt out"),
        ],
        "favours": change.favours,
        "material": change.material is None or change.partial or change.material >= settings.material_threshold,
        "benchmark": await _benchmark(request, change),
        "evidence": _evidence_file(conn, change) is not None,
        "contradictions": _contradictions(conn, [svc.id]),
    })


@router.get("/changes/{change_id}/evidence.png")
async def evidence(request: Request, change_id: int) -> Response:
    conn = _conn(request)
    user = _member(request)
    if user is None:
        return _denied(request, SIGNED_OUT)
    change, _ = _watched_change(conn, user, change_id)
    path = _evidence_file(conn, change)
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/png", headers=PRIVATE_HEADERS)


# --- Swarm telemetry (SSE) -----------------------------------------------------------------------

async def swarm_stream(
    conn: sqlite3.Connection,
    is_disconnected: Callable[[], Awaitable[bool]],
    after_id: int | None = None,
    poll: float | None = None,
) -> AsyncIterator[str]:
    """SSE frames for new `swarm_events` rows, polled by id, until the client goes away."""
    poll = SSE_POLL_SECONDS if poll is None else poll
    if after_id is None:
        last = conn.execute("SELECT MAX(id) FROM swarm_events").fetchone()[0] or 0
    else:
        last = after_id
    yield "retry: 5000\n\n"
    idle = 0
    while not await is_disconnected():
        events = _swarm_events(conn, after_id=last, limit=SSE_BATCH)
        for e in events:
            last = e["id"]
            payload = {k: e[k] for k in ("role", "event", "detail", "duration_ms", "at")}
            yield f"id: {e['id']}\nevent: swarm\ndata: {json.dumps(payload)}\n\n"
        if events:
            idle = 0
            continue
        idle += 1
        if idle >= SSE_HEARTBEAT_POLLS:
            idle = 0
            yield ": ping\n\n"
        await asyncio.sleep(poll)


@router.get("/api/swarm/stream")
async def swarm(request: Request) -> Response:
    if _member(request) is None:
        return Response(status_code=401, headers=PRIVATE_HEADERS)
    last_id = request.headers.get("last-event-id", "")
    after = int(last_id) if last_id.isdigit() else None
    return StreamingResponse(
        swarm_stream(_conn(request), request.is_disconnected, after_id=after),
        media_type="text/event-stream",
        headers=PRIVATE_HEADERS | {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Member Control Room -------------------------------------------------------------------------

DENIED = "This sign-in link has expired or was already used. Send /dashboard to the bot in Telegram for a fresh link."
SIGNED_OUT = "You're not signed in. Send /dashboard to the bot in Telegram for a sign-in link."


def _denied(request: Request, message: str) -> HTMLResponse:
    response = _page(request, "denied.html", {"message": message}, status=401, headers=PRIVATE_HEADERS)
    response.delete_cookie(sessions.COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.get("/auth/magic")
async def magic(request: Request, token: str = "") -> Response:
    cookie = sessions.exchange(_conn(request), token)
    if cookie is None:
        return _denied(request, DENIED)
    response = RedirectResponse("/room", status_code=303, headers=PRIVATE_HEADERS)
    response.set_cookie(
        sessions.COOKIE, cookie, max_age=int(sessions.SESSION_TTL.total_seconds()), path="/",
        secure=True, httponly=True, samesite="lax",
    )
    return response


@router.post("/auth/logout")
async def logout(request: Request) -> Response:
    sessions.end(_conn(request), request.cookies.get(sessions.COOKIE))
    response = RedirectResponse("/", status_code=303, headers=PRIVATE_HEADERS)
    response.delete_cookie(sessions.COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.get("/room", response_class=HTMLResponse)
async def room(request: Request) -> HTMLResponse:
    conn = _conn(request)
    user = sessions.member(conn, request.cookies.get(sessions.COOKIE))
    if user is None:
        return _denied(request, SIGNED_OUT)
    services = agent.services_for(conn, user.id)
    ids = [s.id for s in services]
    receipts = rows(ActionReceipt, conn.execute(
        "SELECT * FROM action_receipts WHERE user_id = ? ORDER BY executed_at DESC LIMIT 50", (user.id,)
    ))
    return _page(request, "room.html", {
        "user": user,
        "board": _radar(conn, services, datetime.now(UTC)),
        "changes": _recent_changes(conn, ids),
        "contradictions": _contradictions(conn, ids),
        "recent_days": RECENT_DAYS,
        "deadlines": ledger.upcoming(conn, user.id),
        "calendar": f"/calendar.ics?token={user.calendar_token}",
        "receipts": receipts,
        "events": _swarm_events(conn),
    }, headers=PRIVATE_HEADERS)

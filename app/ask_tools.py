"""Plain lookups the researcher agent calls, plus the evidence it hands back.

Every lookup takes the member's watchlist from `AskDeps.user_id`: a service the member does not watch
behaves as if it did not exist. No model code lives here, and nothing logs terms text or queries.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app import memory
from app.agent import past_material_changes, services_for
from app.db import Change, Service, one
from app.gateway import Gateway
from app.grounding import contains, normalise
from app.watch import Section, sections

log = logging.getLogger(__name__)

MAX_SECTION_CHARS = 8000
MAX_HITS = 8
SNIPPET_CHARS = 300
MAX_CHANGES = 10
MAX_CHANGE_CHARS = 2000


@dataclass
class AskDeps:
    conn: sqlite3.Connection
    user_id: int
    pinecone: Gateway | None = None


class Clause(BaseModel):
    service: str
    heading: str
    quote: str
    source: Literal["current", "change"] = "current"
    change_id: int | None = None


class Evidence(BaseModel):
    clauses: list[Clause] = Field(default_factory=list)
    answerable: bool = True
    missing: str = ""


class Hit(BaseModel):
    service: str
    heading: str
    snippet: str


class ChangeHit(BaseModel):
    change_id: int
    heading: str
    effective_on: date
    summary: str
    after: str


# --- Scoping ---------------------------------------------------------------------------------------

def _watched(deps: AskDeps, name: str) -> Service | None:
    wanted = name.strip().casefold()
    return next((s for s in services_for(deps.conn, deps.user_id) if s.name.casefold() == wanted), None)


def _latest(deps: AskDeps, service: Service) -> tuple[str, datetime] | None:
    row = deps.conn.execute(
        "SELECT text, taken_at FROM snapshots WHERE service_id = ? ORDER BY taken_at DESC, id DESC LIMIT 1",
        (service.id,),
    ).fetchone()
    return (row["text"], datetime.fromisoformat(row["taken_at"])) if row else None


def _sections_of(deps: AskDeps, service: Service) -> list[Section]:
    latest = _latest(deps, service)
    return sections(latest[0]) if latest else []


def _owned_change(deps: AskDeps, change_id: int) -> tuple[Service, Change] | None:
    change = one(Change, deps.conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,)))
    if change is None:
        return None
    service = next((s for s in services_for(deps.conn, deps.user_id) if s.id == change.service_id), None)
    return (service, change) if service else None


# --- Lookups ---------------------------------------------------------------------------------------

def list_services(deps: AskDeps) -> list[str]:
    return [s.name for s in services_for(deps.conn, deps.user_id)]


def list_sections(deps: AskDeps, service: str) -> list[str]:
    found = _watched(deps, service)
    return [s.heading for s in _sections_of(deps, found)] if found else []


def _section_text(deps: AskDeps, service: str, heading: str) -> str:
    """The whole stored text of a watched service's section (duplicate headings joined), uncapped."""
    found = _watched(deps, service)
    if not found:
        return ""
    wanted = heading.strip().casefold()
    return "\n\n".join(s.body for s in _sections_of(deps, found) if s.heading.strip().casefold() == wanted)


def _split(text: str, limit: int = MAX_SECTION_CHARS) -> list[str]:
    """Parts of at most `limit` characters, broken at blank lines where possible.

    The parts hold every character of `text`; a break at a blank line keeps the blank line at the
    start of the next part, and a paragraph longer than `limit` is cut where it has to be.
    """
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit + 1)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:]
    if text or not parts:
        parts.append(text)
    return parts


def read_section(deps: AskDeps, service: str, heading: str, part: int = 1) -> str:
    """One part of a section; a section longer than MAX_SECTION_CHARS starts with `[part N of M]`."""
    parts = _split(_section_text(deps, service, heading))
    if not 1 <= part <= len(parts):
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"[part {part} of {len(parts)}]\n{parts[part - 1]}"


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", text.lower()))


def _snippet(body: str, wanted: set[str]) -> str:
    """Up to SNIPPET_CHARS of `body` around the first word of the query, else its start."""
    at = next((m.start() for m in re.finditer(r"[A-Za-z]{3,}", body) if m.group().lower() in wanted), None)
    if at is None:
        return body[:SNIPPET_CHARS]
    start = max(0, min(at - SNIPPET_CHARS // 2, len(body) - SNIPPET_CHARS))
    return body[start:start + SNIPPET_CHARS]


def search_terms(deps: AskDeps, query: str) -> list[Hit]:
    wanted = _words(query)
    if not wanted:
        return []
    scored: list[tuple[int, Hit]] = []
    for service in services_for(deps.conn, deps.user_id):
        for section in _sections_of(deps, service):
            score = len(wanted & _words(section.body)) + 2 * len(wanted & _words(section.heading))
            if score > 0:
                hit = Hit(service=service.name, heading=section.heading, snippet=_snippet(section.body, wanted))
                scored.append((score, hit))
    scored.sort(key=lambda pair: -pair[0])  # stable: ties keep watchlist and document order
    return [hit for _, hit in scored[:MAX_HITS]]


def recent_changes(deps: AskDeps, service: str) -> list[ChangeHit]:
    found = _watched(deps, service)
    if not found:
        return []
    return [
        ChangeHit(
            change_id=c.id, heading=c.heading, effective_on=c.effective_at.date(),
            summary=c.summary or "", after=c.after_text[:MAX_CHANGE_CHARS],
        )
        for c in past_material_changes(deps.conn, found.id)[-MAX_CHANGES:]  # oldest first: keep the latest
    ]


async def similar_clauses(deps: AskDeps, text: str) -> list[Hit]:
    if deps.pinecone is None:
        return []
    try:
        found = await memory.similar(
            deps.pinecone, text, exclude_service="", services=list_services(deps), log_body=False
        )
    except Exception as exc:  # noqa: BLE001 — precedents are optional; a failure must not sink the answer
        log.warning("similar clauses lookup failed: %s", type(exc).__name__)
        return []
    return [Hit(service=p.service, heading=p.heading, snippet=p.text[:SNIPPET_CHARS]) for p in found]


# --- Checking the researcher's evidence ------------------------------------------------------------

def _attributed(deps: AskDeps, clause: Clause) -> Clause | None:
    """The clause relabelled from the stored row its quote came from, or None if the quote is not there."""
    if clause.source == "current":
        service = _watched(deps, clause.service)
        if service is None or not contains(clause.quote, _section_text(deps, clause.service, clause.heading)):
            return None
        return clause.model_copy(update={"service": service.name})
    owned = _owned_change(deps, clause.change_id) if clause.change_id is not None else None
    if owned is None:
        return None
    service, change = owned
    if not contains(clause.quote, change.after_text):  # removed wording is not the terms any more
        return None
    return clause.model_copy(update={"service": service.name, "heading": change.heading})


def verified(deps: AskDeps, clauses: list[Clause]) -> list[Clause]:
    """Only clauses whose quote really appears in the member's own stored text, without duplicates.

    Service and heading of a change clause, and the service name of a current one, come from the
    database, never from the caller.
    """
    kept: list[Clause] = []
    seen: set[tuple[str, str, str]] = set()
    for clause in clauses:
        checked = _attributed(deps, clause)
        if checked is None:
            continue
        key = (checked.service.casefold(), checked.heading.strip().casefold(), normalise(checked.quote))
        if key in seen:
            continue
        seen.add(key)
        kept.append(checked)
    return kept


def dated(deps: AskDeps, clause: Clause) -> date | None:
    if clause.source == "current":
        found = _watched(deps, clause.service)
        latest = _latest(deps, found) if found else None
        return latest[1].date() if latest else None
    owned = _owned_change(deps, clause.change_id) if clause.change_id is not None else None
    return owned[1].effective_at.date() if owned else None

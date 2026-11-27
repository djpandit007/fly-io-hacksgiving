"""Plain lookups the researcher agent calls, plus the evidence it hands back.

Every lookup takes the member's watchlist from `AskDeps.user_id`: a service the member does not watch
behaves as if it did not exist. Lookups span every document of a service's Policy Bundle (terms,
privacy, ai_terms, billing, dpa); a document is named by its role, and the terms are the default.
No model code lives here, and nothing logs terms text or queries.
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
from app.agent import past_material_changes, role_of, services_for
from app.db import DOC_ROLE_LABELS, DOC_ROLES, Change, Service, one
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
    document: str = "terms"  # role of the bundle document quoted: terms | privacy | ai_terms | billing | dpa


class Evidence(BaseModel):
    clauses: list[Clause] = Field(default_factory=list)
    answerable: bool = True
    missing: str = ""
    # The service the question is about when the member does not watch it; checked by `ask` before use.
    unwatched_service: str = ""


class Hit(BaseModel):
    service: str
    heading: str
    snippet: str
    document: str = "terms"


class ChangeHit(BaseModel):
    change_id: int
    heading: str
    effective_on: date
    summary: str
    after: str
    document: str = "terms"


# --- Scoping ---------------------------------------------------------------------------------------

def _watched(deps: AskDeps, name: str) -> Service | None:
    wanted = name.strip().casefold()
    return next((s for s in services_for(deps.conn, deps.user_id) if s.name.casefold() == wanted), None)


# Names a model may use for a document, casefolded: the role key or its label, plus common names.
_ROLE_NAMES: dict[str, str] = {
    **{r: r for r in DOC_ROLES},
    **{label.casefold(): r for r, label in DOC_ROLE_LABELS.items()},
    "terms of service": "terms", "terms of use": "terms", "tos": "terms", "privacy": "privacy",
    "privacy notice": "privacy", "ai terms": "ai_terms", "ai": "ai_terms", "billing": "billing",
    "data processing addendum": "dpa", "data processing agreement": "dpa",
}


def role(document: str | None) -> str | None:
    """The role a document name stands for ("AI Terms" -> "ai_terms"; empty -> "terms"), or None."""
    if document is None or not document.strip():
        return "terms"
    name = document.strip().casefold()
    return _ROLE_NAMES.get(name) or _ROLE_NAMES.get(" ".join(name.replace("-", " ").split()))


def _latest(deps: AskDeps, service: Service, document: str = "terms") -> tuple[str, datetime] | None:
    """The newest stored text of one bundle document. Snapshots without a document are the terms."""
    if document == "terms":
        scope = ("(document_id IS NULL OR document_id IN "
                 "(SELECT id FROM service_documents WHERE service_id = ? AND role = 'terms'))")
        args: tuple = (service.id, service.id)
    else:
        scope = "document_id IN (SELECT id FROM service_documents WHERE service_id = ? AND role = ?)"
        args = (service.id, service.id, document)
    row = deps.conn.execute(
        f"SELECT text, taken_at FROM snapshots WHERE service_id = ? AND {scope} ORDER BY taken_at DESC, id DESC LIMIT 1",
        args,
    ).fetchone()
    return (row["text"], datetime.fromisoformat(row["taken_at"])) if row else None


def _documents(deps: AskDeps, service: Service) -> list[str]:
    """The roles of a service's bundle documents that have stored text, terms first."""
    stored = {r["role"] for r in deps.conn.execute(
        """SELECT DISTINCT d.role FROM service_documents d JOIN snapshots s ON s.document_id = d.id
           WHERE d.service_id = ?""",
        (service.id,),
    )}
    if _latest(deps, service) is not None:
        stored.add("terms")
    return [r for r in DOC_ROLES if r in stored]


def _sections_of(deps: AskDeps, service: Service, document: str = "terms") -> list[Section]:
    latest = _latest(deps, service, document)
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


def list_documents(deps: AskDeps, service: str) -> list[str]:
    """The roles of a watched service's documents that can be read (terms, privacy, ai_terms, ...)."""
    found = _watched(deps, service)
    return _documents(deps, found) if found else []


def list_sections(deps: AskDeps, service: str, document: str = "terms") -> list[str]:
    found, wanted = _watched(deps, service), role(document)
    return [s.heading for s in _sections_of(deps, found, wanted)] if found and wanted else []


def _section_bodies(deps: AskDeps, service: str, heading: str, document: str = "terms") -> list[str]:
    """The stored text of each section with this heading in one document of a watched service, in
    document order."""
    found, wanted_doc = _watched(deps, service), role(document)
    if not found or not wanted_doc:
        return []
    wanted = heading.strip().casefold()
    return [s.body for s in _sections_of(deps, found, wanted_doc) if s.heading.strip().casefold() == wanted]


def _section_text(deps: AskDeps, service: str, heading: str, document: str = "terms") -> str:
    """The whole stored text of a watched service's section (duplicate headings joined), uncapped."""
    return "\n\n".join(_section_bodies(deps, service, heading, document))


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


def read_section(deps: AskDeps, service: str, heading: str, part: int = 1, document: str = "terms") -> str:
    """One part of a section; a section longer than MAX_SECTION_CHARS starts with `[part N of M]`."""
    parts = _split(_section_text(deps, service, heading, document))
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
        for document in _documents(deps, service):
            for section in _sections_of(deps, service, document):
                score = len(wanted & _words(section.body)) + 2 * len(wanted & _words(section.heading))
                if score > 0:
                    hit = Hit(service=service.name, heading=section.heading, document=document,
                              snippet=_snippet(section.body, wanted))
                    scored.append((score, hit))
    scored.sort(key=lambda pair: -pair[0])  # stable: ties keep watchlist, bundle and document order
    return [hit for _, hit in scored[:MAX_HITS]]


def recent_changes(deps: AskDeps, service: str) -> list[ChangeHit]:
    found = _watched(deps, service)
    if not found:
        return []
    return [
        ChangeHit(
            change_id=c.id, heading=c.heading, effective_on=c.effective_at.date(),
            summary=c.summary or "", after=c.after_text[:MAX_CHANGE_CHARS],
            document=role_of(deps.conn, c.document_id),
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
        service, document = _watched(deps, clause.service), role(clause.document)
        if service is None or document is None:
            return None
        # Each same-named section on its own: a quote across two of them is not text of the document.
        bodies = _section_bodies(deps, clause.service, clause.heading, document)
        if not any(contains(clause.quote, body) for body in bodies):
            return None
        return clause.model_copy(update={"service": service.name, "document": document})
    owned = _owned_change(deps, clause.change_id) if clause.change_id is not None else None
    if owned is None:
        return None
    service, change = owned
    if not contains(clause.quote, change.after_text):  # removed wording is not the document any more
        return None
    document = role_of(deps.conn, change.document_id)
    latest = _latest(deps, service, document)
    if latest is None or not contains(clause.quote, latest[0]):  # nor is wording a later version dropped
        return None
    return clause.model_copy(update={"service": service.name, "heading": change.heading, "document": document})


def verified(deps: AskDeps, clauses: list[Clause]) -> list[Clause]:
    """Only clauses whose quote really appears in the member's own stored text, without duplicates.

    Service, document and heading of a change clause, and the service name and document of a current
    one, come from the database, never from the caller.
    """
    kept: list[Clause] = []
    seen: set[tuple[str, str, str, str]] = set()
    for clause in clauses:
        checked = _attributed(deps, clause)
        if checked is None:
            continue
        key = (checked.service.casefold(), checked.document, checked.heading.strip().casefold(),
               normalise(checked.quote))
        if key in seen:
            continue
        seen.add(key)
        kept.append(checked)
    return kept


def dated(deps: AskDeps, clause: Clause) -> date | None:
    if clause.source == "current":
        found, document = _watched(deps, clause.service), role(clause.document)
        latest = _latest(deps, found, document) if found and document else None
        return latest[1].date() if latest else None
    owned = _owned_change(deps, clause.change_id) if clause.change_id is not None else None
    return owned[1].effective_at.date() if owned else None

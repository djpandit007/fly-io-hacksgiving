"""SQLite storage on the Sprite's persistent disk."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

# Version 1: the single-owner schema. Kept as the base so every database, new or old, walks the same
# migration path to the current version.
SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    terms_url   TEXT NOT NULL,
    domain      TEXT,
    source      TEXT NOT NULL DEFAULT 'manual',   -- manual | gmail | telegram
    status      TEXT NOT NULL DEFAULT 'active',   -- active | proposed | paused
    tosdr_grade TEXT,
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id),
    taken_at    TEXT NOT NULL,                    -- when the text was in force
    origin      TEXT NOT NULL,                    -- live | ota | wayback
    sha         TEXT NOT NULL,
    text        TEXT NOT NULL,
    UNIQUE (service_id, sha)
);

CREATE TABLE IF NOT EXISTS changes (
    id           INTEGER PRIMARY KEY,
    service_id   INTEGER NOT NULL REFERENCES services(id),
    from_snap    INTEGER REFERENCES snapshots(id),
    to_snap      INTEGER NOT NULL REFERENCES snapshots(id),
    detected_at  TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    heading      TEXT NOT NULL,
    kind         TEXT NOT NULL,                   -- added | removed | modified
    before_text  TEXT NOT NULL,
    after_text   TEXT NOT NULL,
    material     REAL,
    category     TEXT,
    opt_out      REAL,
    severity     REAL,
    summary      TEXT,
    notified_at  TEXT
);

CREATE TABLE IF NOT EXISTS ledger (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    due_on      TEXT NOT NULL,                    -- ISO date
    kind        TEXT NOT NULL,                    -- opt_out | renewal | trial | return | notice | other
    service_id  INTEGER REFERENCES services(id),
    change_id   INTEGER REFERENCES changes(id),
    created_at  TEXT NOT NULL,
    nudged_at   TEXT,
    UNIQUE (title, due_on)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


SCHEMA_VERSION = 8

# Version 2: members, per-member watchlists and deadlines, email dedupe, replayable verdicts.
V2_MIGRATION = """
CREATE TABLE users (
    id                  INTEGER PRIMARY KEY,
    chat_id             INTEGER UNIQUE,                   -- Telegram chat; NULL for email-only members
    email               TEXT UNIQUE,                      -- verified address only
    email_pending       TEXT,
    email_code_hash     TEXT,
    email_code_expires  TEXT,
    email_code_attempts INTEGER NOT NULL DEFAULT 0,
    role                TEXT NOT NULL DEFAULT 'member',   -- owner | member
    name                TEXT NOT NULL DEFAULT '',
    calendar_token      TEXT NOT NULL UNIQUE,
    joined_at           TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active'    -- active | removed
);
CREATE UNIQUE INDEX users_one_owner ON users(role) WHERE role = 'owner';

CREATE TABLE subscriptions (
    user_id    INTEGER NOT NULL REFERENCES users(id),
    service_id INTEGER NOT NULL REFERENCES services(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, service_id)
);

CREATE TABLE mail_seen (
    message_id TEXT PRIMARY KEY,
    seen_at    TEXT NOT NULL
);

CREATE TABLE ledger_v2 (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id),             -- NULL: pre-v2 row, adopted by the owner
    title       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    due_on      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    service_id  INTEGER REFERENCES services(id),
    change_id   INTEGER REFERENCES changes(id),
    created_at  TEXT NOT NULL,
    nudged_at   TEXT,
    UNIQUE (user_id, title, due_on)
);
INSERT INTO ledger_v2 (id, user_id, title, detail, due_on, kind, service_id, change_id, created_at, nudged_at)
    SELECT id, NULL, title, detail, due_on, kind, service_id, change_id, created_at, nudged_at FROM ledger;
DROP TABLE ledger;
ALTER TABLE ledger_v2 RENAME TO ledger;

ALTER TABLE changes ADD COLUMN favours TEXT;
ALTER TABLE changes ADD COLUMN partial INTEGER NOT NULL DEFAULT 0;
"""


# Version 3: rate limits and lockout for email verification codes.
V3_MIGRATION = """
CREATE TABLE email_codes (
    id       INTEGER PRIMARY KEY,
    user_id  INTEGER NOT NULL REFERENCES users(id),
    address  TEXT NOT NULL,
    sent_at  TEXT NOT NULL
);
CREATE INDEX email_codes_address ON email_codes(address, sent_at);
CREATE INDEX email_codes_user ON email_codes(user_id, sent_at);
ALTER TABLE users ADD COLUMN email_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN email_locked_until TEXT;
"""

# Version 4: owner metrics. Counts, durations and provider-reported amounts only: never URLs, file
# names, message text, addresses or member ids (see app/metrics.py).
V4_MIGRATION = """
CREATE TABLE metric_runs (
    id          INTEGER PRIMARY KEY,
    ran_at      TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    checked     INTEGER NOT NULL,
    failed      TEXT NOT NULL,                    -- JSON list of service names that could not be checked
    changes     INTEGER NOT NULL,
    nudges      INTEGER NOT NULL,
    sent        INTEGER NOT NULL,
    undelivered INTEGER NOT NULL
);
CREATE TABLE metric_failures (
    id     INTEGER PRIMARY KEY,
    at     TEXT NOT NULL,
    kind   TEXT NOT NULL,                         -- telegram | mail
    error  TEXT NOT NULL                          -- exception type only
);
CREATE TABLE metric_counts (
    day      TEXT NOT NULL,                       -- UTC date
    kind     TEXT NOT NULL,                       -- provider | command | job
    name     TEXT NOT NULL,
    outcome  TEXT NOT NULL,                       -- ok | error | retried
    n        INTEGER NOT NULL DEFAULT 0,
    total_ms INTEGER NOT NULL DEFAULT 0,
    max_ms   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind, name, outcome)
);
CREATE TABLE metric_usage (
    day      TEXT NOT NULL,
    provider TEXT NOT NULL,
    unit     TEXT NOT NULL,                       -- usd | input_tokens | output_tokens | credits | read_units | embed_tokens
    amount   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider, unit)
);
"""

# Version 5: answer feedback, and the unwatches a member may undo (Undo restores only those).
# Answer feedback: One row per rated answer, found by an unguessable token. Metadata only:
# never the answer's text, a URL, or which member received or rated it (see app/feedback.py).
V5_MIGRATION = """
CREATE TABLE answers (
    token          TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,                 -- verdict | alert | overview | wrapped | ask
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    change_id      INTEGER,                       -- the public terms change an alert or overview covers
    quotes_ok      INTEGER,                       -- verdicts: 1 all clause quotes passed, 0 some dropped, NULL none asked
    sent_at        TEXT NOT NULL,
    vote           TEXT,                          -- up | down
    reason         TEXT,                          -- wrong | unclear | not_useful
    voted_at       TEXT
);
CREATE INDEX answers_sent ON answers(sent_at);
CREATE TABLE unwatched (
    user_id      INTEGER NOT NULL REFERENCES users(id),
    service_id   INTEGER NOT NULL REFERENCES services(id),
    unwatched_at TEXT NOT NULL,
    PRIMARY KEY (user_id, service_id)
);
"""

# Version 6: Policy Bundles (several legal documents per service), cross-document contradictions,
# member jurisdiction, single-use approval tokens, metadata-only action receipts, and sanitized
# swarm telemetry. Adds tables and nullable columns only, so restoring the
# .v5.bak copy rolls back cleanly. Uploaded member contracts and notice bodies are never stored.
V6_MIGRATION = """
CREATE TABLE service_documents (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id),
    role        TEXT NOT NULL,                    -- terms | privacy | ai_terms | billing | dpa
    url         TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    UNIQUE (service_id, role),
    UNIQUE (service_id, url)
);
INSERT INTO service_documents (service_id, role, url, added_at)
    SELECT id, 'terms', terms_url, added_at FROM services;
ALTER TABLE snapshots ADD COLUMN document_id INTEGER REFERENCES service_documents(id);
ALTER TABLE snapshots ADD COLUMN evidence_path TEXT;
ALTER TABLE changes ADD COLUMN document_id INTEGER REFERENCES service_documents(id);
CREATE TABLE document_contradictions (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id),
    doc_a_id    INTEGER NOT NULL REFERENCES service_documents(id),
    doc_b_id    INTEGER NOT NULL REFERENCES service_documents(id),
    heading_a   TEXT NOT NULL,
    heading_b   TEXT NOT NULL,
    quote_a     TEXT NOT NULL,
    quote_b     TEXT NOT NULL,
    summary     TEXT NOT NULL,
    severity    REAL NOT NULL,
    detected_at TEXT NOT NULL,
    UNIQUE (service_id, doc_a_id, doc_b_id, heading_a, heading_b)
);
ALTER TABLE users ADD COLUMN jurisdiction TEXT;
CREATE TABLE approval_tokens (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    action_kind  TEXT NOT NULL,                   -- opt_out_email | opt_out_form | redline_kit
    payload_json TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    consumed_at  TEXT
);
CREATE TABLE action_receipts (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    service_name TEXT NOT NULL,
    action_kind  TEXT NOT NULL,                   -- opt_out_email | opt_out_form | gdpr_delete | ai_opt_out
    target       TEXT NOT NULL,                   -- recipient address or form host
    clause_sha   TEXT NOT NULL,
    proof_ref    TEXT NOT NULL DEFAULT '',        -- AgentMail message id or screenshot sha256
    executed_at  TEXT NOT NULL
);
CREATE TABLE swarm_events (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    sprite_role TEXT NOT NULL,                    -- gateway | scout | advocate
    event       TEXT NOT NULL,                    -- wake | egress_lock | run | checkpoint_restore | sleep | error
    detail      TEXT NOT NULL DEFAULT '',         -- generic label only, never URLs or member data
    duration_ms INTEGER
);
CREATE INDEX swarm_events_at ON swarm_events(at)
"""

# Version 7: snapshots are unique per document, not per service. The v1 UNIQUE (service_id, sha) dropped a
# document's return to earlier wording (a walked-back clause was never stored or alerted, and the next
# change was diffed against stale text) and the second of two documents with identical text. Rows from
# before v6 (no document) are given their service's terms document, then the table is rebuilt (SQLite
# can't drop a constraint); _migrate turns foreign keys off around a rebuild and checks them before commit.
V7_MIGRATION = """
UPDATE snapshots SET document_id = (
    SELECT d.id FROM service_documents d WHERE d.service_id = snapshots.service_id AND d.role = 'terms'
) WHERE document_id IS NULL;
CREATE TABLE snapshots_v7 (
    id            INTEGER PRIMARY KEY,
    service_id    INTEGER NOT NULL REFERENCES services(id),
    taken_at      TEXT NOT NULL,                  -- when the text was in force
    origin        TEXT NOT NULL,                  -- live | ota | wayback
    sha           TEXT NOT NULL,
    text          TEXT NOT NULL,
    document_id   INTEGER REFERENCES service_documents(id),
    evidence_path TEXT,
    UNIQUE (document_id, taken_at, sha)
);
INSERT INTO snapshots_v7 (id, service_id, taken_at, origin, sha, text, document_id, evidence_path)
    SELECT id, service_id, taken_at, origin, sha, text, document_id, evidence_path FROM snapshots;
DROP TABLE snapshots;
ALTER TABLE snapshots_v7 RENAME TO snapshots;
CREATE INDEX snapshots_document_taken ON snapshots(document_id, taken_at)
"""

# Version 8: how each live version was read (provenance), and the census of popular services'
# standing terms: one row per (registrable domain, document role, red flag), found or not.
V8_MIGRATION = """
ALTER TABLE snapshots ADD COLUMN provenance TEXT;
CREATE TABLE census (
    domain      TEXT NOT NULL,
    role        TEXT NOT NULL,
    flag        TEXT NOT NULL,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    p           REAL NOT NULL,
    clause      TEXT,
    severity    REAL,
    has_opt_out INTEGER NOT NULL DEFAULT 0,
    sha         TEXT NOT NULL,
    graded_at   TEXT NOT NULL,
    PRIMARY KEY (domain, role, flag)
)
"""

MIGRATIONS = {
    2: V2_MIGRATION, 3: V3_MIGRATION, 4: V4_MIGRATION, 5: V5_MIGRATION, 6: V6_MIGRATION, 7: V7_MIGRATION,
    8: V8_MIGRATION,
}
REBUILDS = frozenset({7})  # migrations that rebuild a table other tables reference

DOC_ROLES = ("terms", "privacy", "ai_terms", "billing", "dpa")
DocRole = Literal["terms", "privacy", "ai_terms", "billing", "dpa"]
DOC_ROLE_LABELS: dict[str, str] = {
    "terms": "Terms",
    "privacy": "Privacy Policy",
    "ai_terms": "AI Terms",
    "billing": "Billing Terms",
    "dpa": "DPA",
}


def backup_path(path: Path, version: int) -> Path:
    return path.with_name(f"{path.name}.v{version}.bak")


def _migrate(conn: sqlite3.Connection, path: Path) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    has_data = conn.execute("SELECT COUNT(*) FROM services").fetchone()[0] > 0
    if has_data and str(path) != ":memory:":
        # Keep a copy of the pre-migration database for rollback.
        with sqlite3.connect(backup_path(path, max(version, 1))) as bak:
            conn.backup(bak)
    for target in range(max(version, 1) + 1, SCHEMA_VERSION + 1):
        # executescript commits first, so run the statements one by one inside our own transaction.
        statements = [s.strip() for s in MIGRATIONS[target].split(";\n") if s.strip()]
        rebuild = target in REBUILDS
        if rebuild:
            # SQLite's table-rebuild procedure: foreign keys off (it can't change inside a transaction),
            # so dropping the old table neither fails nor cascades; every reference is checked before commit.
            conn.execute("PRAGMA foreign_keys = OFF")
        try:
            with transaction(conn):
                for stmt in statements:
                    conn.execute(stmt)
                if rebuild and conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise sqlite3.IntegrityError(f"migration {target} left a broken reference")
                conn.execute(f"PRAGMA user_version = {target}")
        finally:
            if rebuild:
                conn.execute("PRAGMA foreign_keys = ON")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn, path)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def get_kv(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --- Typed rows ----------------------------------------------------------------------------------

class Row(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=False)


class Service(Row):
    id: int
    name: str
    terms_url: str
    domain: str | None = None
    tosdr_grade: str | None = None
    added_at: datetime
    # `source` and `status` remain as unused v1 columns; watch state lives in `subscriptions`.


class Snapshot(Row):
    id: int
    service_id: int
    taken_at: datetime
    origin: Literal["live", "ota", "wayback"]
    sha: str
    text: str
    document_id: int | None = None   # NULL: the service's primary terms document (pre-v6 rows)
    evidence_path: str | None = None  # relative path of a public evidentiary PNG, if Scout captured one
    provenance: str | None = None     # how a live version was read; NULL for rows from before v8


class Change(Row):
    id: int
    service_id: int
    from_snap: int | None
    to_snap: int
    detected_at: datetime
    effective_at: datetime
    heading: str
    kind: Literal["added", "removed", "modified"]
    before_text: str
    after_text: str
    material: float | None = None
    category: str | None = None
    opt_out: float | None = None
    severity: float | None = None
    summary: str | None = None
    notified_at: datetime | None = None
    favours: str | None = None
    partial: bool = False
    document_id: int | None = None


class LedgerEntry(Row):
    id: int
    user_id: int | None = None
    title: str
    detail: str = ""
    due_on: date
    kind: Literal["opt_out", "renewal", "trial", "return", "notice", "payment", "other"]
    service_id: int | None = None
    change_id: int | None = None
    created_at: datetime
    nudged_at: datetime | None = None


class User(Row):
    id: int
    chat_id: int | None = None
    email: str | None = None
    email_pending: str | None = None
    email_code_hash: str | None = None
    email_code_expires: datetime | None = None
    email_code_attempts: int = 0
    role: Literal["owner", "member"] = "member"
    name: str = ""
    calendar_token: str
    joined_at: datetime
    status: Literal["active", "removed", "pending"] = "active"  # pending: email-only, address unconfirmed
    email_failures: int = 0
    email_locked_until: datetime | None = None
    jurisdiction: str | None = None  # US-CA | US-NY | US-OTHER | EU | UK

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def label(self) -> str:
        return self.name or (f"chat {self.chat_id}" if self.chat_id else self.email or f"user {self.id}")


class Subscription(Row):
    user_id: int
    service_id: int
    created_at: datetime


class ServiceDocument(Row):
    id: int
    service_id: int
    role: DocRole
    url: str
    added_at: datetime

    @property
    def label(self) -> str:
        return DOC_ROLE_LABELS[self.role]


class Contradiction(Row):
    id: int
    service_id: int
    doc_a_id: int
    doc_b_id: int
    heading_a: str
    heading_b: str
    quote_a: str
    quote_b: str
    summary: str
    severity: float
    detected_at: datetime


class ApprovalToken(Row):
    token_hash: str
    user_id: int
    action_kind: Literal["opt_out_email", "opt_out_form", "redline_kit"]
    payload_json: str
    expires_at: datetime
    consumed_at: datetime | None = None


class ActionReceipt(Row):
    id: int
    user_id: int
    service_name: str
    action_kind: Literal["opt_out_email", "opt_out_form", "gdpr_delete", "ai_opt_out"]
    target: str
    clause_sha: str
    proof_ref: str = ""
    executed_at: datetime


class SwarmEvent(Row):
    id: int
    at: datetime
    sprite_role: Literal["gateway", "scout", "advocate"]
    event: Literal["wake", "egress_lock", "run", "checkpoint_restore", "sleep", "error"]
    detail: str = ""
    duration_ms: int | None = None


class CensusRow(Row):
    domain: str
    role: Literal["terms", "privacy"]
    flag: str
    name: str
    url: str
    p: float
    clause: str | None = None
    severity: float | None = None
    has_opt_out: bool = False
    sha: str
    graded_at: datetime


def rows[R: Row](model: type[R], cursor: sqlite3.Cursor) -> list[R]:
    return [model.model_validate(dict(r)) for r in cursor.fetchall()]


def one[R: Row](model: type[R], cursor: sqlite3.Cursor) -> R | None:
    r = cursor.fetchone()
    return model.model_validate(dict(r)) if r else None

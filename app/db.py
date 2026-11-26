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


SCHEMA_VERSION = 4

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

MIGRATIONS = {2: V2_MIGRATION, 3: V3_MIGRATION, 4: V4_MIGRATION}


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
        with transaction(conn):
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {target}")


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



def rows[R: Row](model: type[R], cursor: sqlite3.Cursor) -> list[R]:
    return [model.model_validate(dict(r)) for r in cursor.fetchall()]


def one[R: Row](model: type[R], cursor: sqlite3.Cursor) -> R | None:
    r = cursor.fetchone()
    return model.model_validate(dict(r)) if r else None

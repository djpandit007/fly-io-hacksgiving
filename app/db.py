"""SQLite storage on the Sprite's persistent disk."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

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


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
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
    source: Literal["manual", "gmail", "telegram"] = "manual"
    status: Literal["active", "proposed", "paused"] = "active"
    tosdr_grade: str | None = None
    added_at: datetime


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


class LedgerEntry(Row):
    id: int
    title: str
    detail: str = ""
    due_on: date
    kind: Literal["opt_out", "renewal", "trial", "return", "notice", "payment", "other"]
    service_id: int | None = None
    change_id: int | None = None
    created_at: datetime
    nudged_at: datetime | None = None


R = TypeVar("R", bound=Row)


def rows(model: type[R], cursor: sqlite3.Cursor) -> list[R]:
    return [model.model_validate(dict(r)) for r in cursor.fetchall()]


def one(model: type[R], cursor: sqlite3.Cursor) -> R | None:
    r = cursor.fetchone()
    return model.model_validate(dict(r)) if r else None

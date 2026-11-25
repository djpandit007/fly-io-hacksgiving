"""Dated obligations: opt-out windows, renewals, trials, return windows, notice periods.

Each member has their own ledger, served as a private iCalendar feed and nudged ahead of time.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

from app.db import LedgerEntry, now, rows

NUDGE_DAYS = 3


def add(
    conn: sqlite3.Connection,
    user_id: int,
    title: str,
    due_on: date,
    kind: str,
    detail: str = "",
    service_id: int | None = None,
    change_id: int | None = None,
) -> bool:
    """Add a deadline to one member's ledger. False when they already have it (same title and date)."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO ledger (user_id, title, detail, due_on, kind, service_id, change_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, title, detail, due_on.isoformat(), kind, service_id, change_id, now()),
    )
    return cur.rowcount == 1


def upcoming(conn: sqlite3.Connection, user_id: int, today: date | None = None, days: int = 365) -> list[LedgerEntry]:
    today = today or date.today()
    return rows(
        LedgerEntry,
        conn.execute(
            "SELECT * FROM ledger WHERE user_id = ? AND due_on >= ? AND due_on <= ? ORDER BY due_on",
            (user_id, today.isoformat(), (today + timedelta(days=days)).isoformat()),
        ),
    )


def due_for_nudge(conn: sqlite3.Connection, today: date | None = None) -> list[LedgerEntry]:
    """Every active member's deadlines due within NUDGE_DAYS that haven't been nudged yet."""
    today = today or date.today()
    return rows(
        LedgerEntry,
        conn.execute(
            """SELECT l.* FROM ledger l JOIN users u ON u.id = l.user_id AND u.status = 'active'
               WHERE l.nudged_at IS NULL AND l.due_on >= ? AND l.due_on <= ? ORDER BY l.user_id, l.due_on""",
            (today.isoformat(), (today + timedelta(days=NUDGE_DAYS)).isoformat()),
        ),
    )


def mark_nudged(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.executemany("UPDATE ledger SET nudged_at = ? WHERE id = ?", [(now(), i) for i in ids])


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: lines longer than 75 octets are folded with CRLF + space."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, chunk = [], b""
    for ch in line:
        b = ch.encode()
        if len(chunk) + len(b) > (75 if not parts else 74):
            parts.append(chunk.decode())
            chunk = b""
        chunk += b
    parts.append(chunk.decode())
    return "\r\n ".join(parts)


def to_ics(entries: list[LedgerEntry], calendar_name: str = "Fine Print deadlines") -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Fine Print//Deadlines//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
    ]
    for row in entries:
        start = row.due_on
        lines += [
            "BEGIN:VEVENT",
            f"UID:fineprint-{row.id}@fineprint",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(start + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_escape(row.title)}",
            f"DESCRIPTION:{_escape(row.detail)}",
            f"CATEGORIES:{_escape(row.kind)}",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_escape(row.title)}",
            f"TRIGGER:-P{NUDGE_DAYS}D",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"

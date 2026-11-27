"""Per-member daily quotas for the heavy worker actions (redline kits, Scout crawls of a member's link).

One kv row per member and action, holding today's date and count, so it resets itself each UTC day and
never grows. The Gateway runs one event loop, and `take` doesn't await, so a check and its increment
can't interleave with another member's."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from app.db import get_kv, set_kv

REDLINE_KITS_PER_DAY = 5   # each kit is two model passes and a document build on the Advocate
SCOUT_CHECKS_PER_DAY = 10  # one-off link checks that fall back to a Scout browser crawl


def _key(name: str, user_id: int) -> str:
    return f"quota:{name}:{user_id}"


def take(conn: sqlite3.Connection, name: str, user_id: int, limit: int, today: date | None = None) -> bool:
    """Use one of the member's `limit` daily `name` actions. False (and nothing used) when none are left."""
    day = (today or datetime.now(UTC).date()).isoformat()
    stored_day, _, count = (get_kv(conn, _key(name, user_id)) or "").partition("|")
    used = int(count) if stored_day == day and count.isdigit() else 0
    if used >= limit:
        return False
    set_kv(conn, _key(name, user_id), f"{day}|{used + 1}")
    return True

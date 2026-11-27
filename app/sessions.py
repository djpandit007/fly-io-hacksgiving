"""Control Room sign-in: single-use magic links from `/dashboard`, exchanged for a signed session cookie.

Only SHA-256 hashes of tokens are stored (`dashboard_sessions`). A magic link lasts 10 minutes and works
once; a session lasts 24 hours. Removing a member deletes their rows, so revocation is immediate, and
every lookup also requires the member to be active.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

from app import users
from app.config import settings
from app.db import User, transaction

MAGIC_TTL = timedelta(minutes=10)
SESSION_TTL = timedelta(hours=24)
COOKIE = "fp_session"


def _stamp(at: datetime) -> str:
    return at.isoformat(timespec="seconds")


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _key() -> bytes:
    return (settings.session_secret.get_secret_value() or settings.shared_secret.get_secret_value()).encode()


def configured() -> bool:
    """Links need somewhere to point and a key to sign sessions with."""
    return bool(settings.public_url) and bool(_key())


def magic_link(conn: sqlite3.Connection, user: User, now: datetime | None = None) -> str:
    """A fresh single-use sign-in link for `user`; any earlier unused link of theirs stops working."""
    now = now or datetime.now(UTC)
    raw = secrets.token_urlsafe(32)
    with transaction(conn):
        conn.execute(
            "DELETE FROM dashboard_sessions WHERE (user_id = ? AND kind = 'magic') OR expires_at <= ?",
            (user.id, _stamp(now)),
        )
        conn.execute(
            "INSERT INTO dashboard_sessions (token_hash, user_id, kind, expires_at) VALUES (?, ?, 'magic', ?)",
            (_hash(raw), user.id, _stamp(now + MAGIC_TTL)),
        )
    return f"{settings.public_url}/auth/magic?token={raw}"


def _sign(raw: str) -> str:
    return hmac.new(_key(), raw.encode(), hashlib.sha256).hexdigest()


def exchange(conn: sqlite3.Connection, token: str, now: datetime | None = None) -> str | None:
    """Consume a magic token (atomically, once) and return a signed session cookie value, or None."""
    if not token or not _key():
        return None
    now = now or datetime.now(UTC)
    token_hash = _hash(token)
    with transaction(conn):
        cur = conn.execute(
            """UPDATE dashboard_sessions SET consumed_at = ?
               WHERE token_hash = ? AND kind = 'magic' AND consumed_at IS NULL AND expires_at > ?""",
            (_stamp(now), token_hash, _stamp(now)),
        )
        if cur.rowcount != 1:
            return None
        user_id = conn.execute("SELECT user_id FROM dashboard_sessions WHERE token_hash = ?", (token_hash,)).fetchone()[0]
        if users.active(users.by_id(conn, user_id)) is None:
            return None
        raw = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO dashboard_sessions (token_hash, user_id, kind, expires_at) VALUES (?, ?, 'session', ?)",
            (_hash(raw), user_id, _stamp(now + SESSION_TTL)),
        )
    return f"{raw}.{_sign(raw)}"


def _raw(cookie: str | None) -> str | None:
    if not cookie or not _key():
        return None
    raw, _, sig = cookie.rpartition(".")
    return raw if raw and hmac.compare_digest(sig, _sign(raw)) else None


def member(conn: sqlite3.Connection, cookie: str | None, now: datetime | None = None) -> User | None:
    """The active member a session cookie belongs to, if it is genuine, current and not revoked."""
    raw = _raw(cookie)
    if raw is None:
        return None
    now = now or datetime.now(UTC)
    row = conn.execute(
        "SELECT user_id FROM dashboard_sessions WHERE token_hash = ? AND kind = 'session' AND expires_at > ?",
        (_hash(raw), _stamp(now)),
    ).fetchone()
    return users.active(users.by_id(conn, row[0])) if row else None


def end(conn: sqlite3.Connection, cookie: str | None) -> None:
    if raw := _raw(cookie):
        conn.execute("DELETE FROM dashboard_sessions WHERE token_hash = ? AND kind = 'session'", (_hash(raw),))

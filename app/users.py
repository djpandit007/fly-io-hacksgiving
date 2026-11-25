"""Who may use Fine Print: one owner, members who joined with the join code, email-only members.

Codes are compared in constant time. Each member has a private calendar token. Data recorded before
multi-user support (services, deadlines, the old owner chat) is adopted by the owner.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import sqlite3

from app.config import settings
from app.db import User, get_kv, now, one, rows, transaction

log = logging.getLogger(__name__)


def _matches(given: str, expected: str) -> bool:
    return bool(expected) and bool(given) and hmac.compare_digest(given.strip(), expected)


def _new_token() -> str:
    return secrets.token_hex(12)


# --- Lookups -------------------------------------------------------------------------------------

def by_id(conn: sqlite3.Connection, user_id: int) -> User | None:
    return one(User, conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)))


def by_chat(conn: sqlite3.Connection, chat_id: int) -> User | None:
    return one(User, conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)))


def by_email(conn: sqlite3.Connection, email: str) -> User | None:
    """The member whose *verified* address this is."""
    return one(User, conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)))


def by_calendar_token(conn: sqlite3.Connection, token: str) -> User | None:
    if not token:
        return None
    user = one(User, conn.execute("SELECT * FROM users WHERE calendar_token = ? AND status = 'active'", (token,)))
    if user is None and _matches(token, settings.calendar_token.get_secret_value()):
        return owner(conn)  # the single-owner version's calendar link keeps working
    return user


def members(conn: sqlite3.Connection) -> list[User]:
    return rows(User, conn.execute("SELECT * FROM users WHERE status = 'active' ORDER BY role DESC, joined_at"))


def active(user: User | None) -> User | None:
    return user if user and user.status == "active" else None


# --- Owner ---------------------------------------------------------------------------------------

def owner(conn: sqlite3.Connection) -> User | None:
    """The owner: the configured chat if set, else the claimed owner, else a pre-v2 owner chat."""
    if settings.owner_chat_id:
        return _ensure_owner(conn, settings.owner_chat_id)
    found = one(User, conn.execute("SELECT * FROM users WHERE role = 'owner'"))
    if found:
        return found
    legacy = get_kv(conn, "owner_chat")
    if legacy:
        return _ensure_owner(conn, int(legacy))
    return None


def _ensure_owner(conn: sqlite3.Connection, chat_id: int, name: str = "") -> User:
    with transaction(conn):
        conn.execute("UPDATE users SET role = 'member' WHERE role = 'owner' AND chat_id IS NOT ?", (chat_id,))
        existing = by_chat(conn, chat_id)
        if existing:
            conn.execute("UPDATE users SET role = 'owner', status = 'active' WHERE id = ?", (existing.id,))
        else:
            conn.execute(
                "INSERT INTO users (chat_id, role, name, calendar_token, joined_at) VALUES (?, 'owner', ?, ?, ?)",
                (chat_id, name, _new_token(), now()),
            )
    user = by_chat(conn, chat_id)
    assert user
    adopt_legacy(conn, user)
    return user


def claim_owner(conn: sqlite3.Connection, chat_id: int, code: str, name: str = "") -> User | None:
    """`/start <owner claim code>` on an unowned bot. None if owned already or the code is wrong."""
    if owner(conn) is not None or not _matches(code, settings.owner_claim_code.get_secret_value()):
        return None
    log.info("bot claimed by chat %s", chat_id)
    return _ensure_owner(conn, chat_id, name)


def adopt_legacy(conn: sqlite3.Connection, owner_user: User) -> None:
    """Give the owner what the single-owner version recorded: watched services and deadlines."""
    with transaction(conn):
        conn.execute(
            """INSERT OR IGNORE INTO subscriptions (user_id, service_id, created_at)
               SELECT ?, s.id, ? FROM services s
               WHERE NOT EXISTS (SELECT 1 FROM subscriptions x WHERE x.service_id = s.id)""",
            (owner_user.id, now()),
        )
        conn.execute("UPDATE OR IGNORE ledger SET user_id = ? WHERE user_id IS NULL", (owner_user.id,))
        conn.execute("DELETE FROM ledger WHERE user_id IS NULL")  # duplicates of rows the owner already has
        conn.execute("DELETE FROM kv WHERE key = 'owner_chat'")


# --- Members -------------------------------------------------------------------------------------

def join_member(
    conn: sqlite3.Connection, code: str, *, chat_id: int | None = None, email: str | None = None, name: str = ""
) -> User | None:
    """Join with the join code by chat, or by (already trusted) email address. Idempotent."""
    if not _matches(code, settings.join_code.get_secret_value()):
        return None
    if chat_id is None and not email:
        raise ValueError("a member needs a chat or an email address")
    email = email.strip().lower() if email else None
    existing = (by_chat(conn, chat_id) if chat_id is not None else None) or (by_email(conn, email) if email else None)
    if existing:
        if existing.status != "active":
            conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (existing.id,))
        return by_id(conn, existing.id)
    cur = conn.execute(
        "INSERT INTO users (chat_id, email, role, name, calendar_token, joined_at) VALUES (?, ?, 'member', ?, ?, ?)",
        (chat_id, email, name, _new_token(), now()),
    )
    log.info("new member %s via %s", cur.lastrowid, "telegram" if chat_id is not None else "email")
    return by_id(conn, cur.lastrowid)


def remove(conn: sqlite3.Connection, user_id: int) -> bool:
    """Owner-only: stop serving a member. The owner cannot be removed."""
    cur = conn.execute("UPDATE users SET status = 'removed' WHERE id = ? AND role = 'member'", (user_id,))
    return cur.rowcount == 1


def set_name(conn: sqlite3.Connection, user: User, name: str) -> None:
    if name and name != user.name:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name[:64], user.id))

"""Who may use Fine Print: one owner, members who joined with the join code, email-only members.

Codes are compared in constant time. Each member has a private calendar token. Data recorded before
multi-user support (services, deadlines, the old owner chat) is adopted by the owner.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Literal

from app import playbooks
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


def set_jurisdiction(conn: sqlite3.Connection, user: User, code: str) -> str | None:
    """Set (or, with "clear", clear) this member's jurisdiction; returns the stored code or None.

    Only the code is stored. Raises ValueError for anything not in playbooks.JURISDICTIONS.
    """
    wanted = code.strip().upper().replace("_", "-")
    if wanted.lower() in playbooks.CLEAR_WORDS:
        stored = None
    elif wanted in playbooks.JURISDICTIONS:
        stored = wanted
    else:
        raise ValueError(f"unsupported jurisdiction {code!r}")
    conn.execute("UPDATE users SET jurisdiction = ? WHERE id = ?", (stored, user.id))
    return stored


# --- Linking an email address ----------------------------------------------------------------------

EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
CODE_TTL = timedelta(minutes=15)
MAX_ATTEMPTS = 5                 # wrong codes per issued code
MAX_FAILURES = 10                # wrong codes in total before a lockout; /email does not reset it
LOCKOUT = timedelta(hours=24)
RESEND_AFTER = timedelta(seconds=60)
MAX_CODES_PER_USER_PER_DAY = 5
MAX_CODES_PER_ADDRESS_PER_DAY = 3

VerifyResult = Literal["ok", "wrong", "expired", "none", "too_many", "in_use", "locked"]


class EmailInUse(Exception):
    pass


class RateLimited(Exception):
    pass


def _code_hash(user_id: int, code: str) -> str:
    return hashlib.sha256(f"{user_id}:{code}".encode()).hexdigest()


def _since(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat()


def _check_limits(conn: sqlite3.Connection, user: User, address: str) -> None:
    now_ = datetime.now(UTC)
    if user.email_locked_until and user.email_locked_until > now_:
        raise RateLimited("too many wrong codes; try again tomorrow")
    last = conn.execute(
        "SELECT MAX(sent_at) FROM email_codes WHERE user_id = ?", (user.id,)
    ).fetchone()[0]
    if last and datetime.fromisoformat(last) > now_ - RESEND_AFTER:
        raise RateLimited("please wait a minute before asking for another code")
    day = _since(timedelta(days=1))
    if conn.execute("SELECT COUNT(*) FROM email_codes WHERE user_id = ? AND sent_at > ?", (user.id, day)).fetchone()[0] >= MAX_CODES_PER_USER_PER_DAY:
        raise RateLimited("that's enough codes for today")
    if conn.execute("SELECT COUNT(*) FROM email_codes WHERE address = ? AND sent_at > ?", (address, day)).fetchone()[0] >= MAX_CODES_PER_ADDRESS_PER_DAY:
        raise RateLimited("that address has had enough codes for today")


def _issue_code(conn: sqlite3.Connection, user: User, address: str) -> str:
    _check_limits(conn, user, address)
    code = f"{secrets.randbelow(1_000_000):06d}"
    conn.execute(
        """UPDATE users SET email_pending = ?, email_code_hash = ?, email_code_expires = ?, email_code_attempts = 0
           WHERE id = ?""",
        (address, _code_hash(user.id, code), (datetime.now(UTC) + CODE_TTL).isoformat(), user.id),
    )
    conn.execute("INSERT INTO email_codes (user_id, address, sent_at) VALUES (?, ?, ?)", (user.id, address, now()))
    return code


def start_email_link(conn: sqlite3.Connection, user: User, address: str) -> str:
    """Begin linking `address` to `user`; returns the 6-digit code to email to that address.

    Raises ValueError (malformed), EmailInUse (verified by another member) or RateLimited.
    """
    address = address.strip().lower()
    if not EMAIL.match(address):
        raise ValueError("not an email address")
    holder = by_email(conn, address)
    if holder and holder.id != user.id:
        raise EmailInUse(address)
    return _issue_code(conn, by_id(conn, user.id) or user, address)


def verify_email(conn: sqlite3.Connection, user: User, code: str) -> VerifyResult:
    fresh = by_id(conn, user.id)
    if fresh and fresh.email_locked_until and fresh.email_locked_until > datetime.now(UTC):
        return "locked"
    if not fresh or not fresh.email_pending or not fresh.email_code_hash:
        return "none"

    def clear() -> None:
        # A pending email-only member keeps its address, so the same row (and its failure count and
        # lockout) is reused by the next join-code email instead of a fresh row resetting them.
        keep_address = fresh.status == "pending"
        conn.execute(
            "UPDATE users SET email_pending = CASE WHEN ? THEN email_pending ELSE NULL END, email_code_hash = NULL, "
            "email_code_expires = NULL, email_code_attempts = 0 WHERE id = ?",
            (keep_address, user.id),
        )

    if fresh.email_code_expires and fresh.email_code_expires < datetime.now(UTC):
        clear()
        return "expired"
    if fresh.email_code_attempts >= MAX_ATTEMPTS:
        clear()
        return "too_many"
    if not hmac.compare_digest(_code_hash(user.id, code.strip()), fresh.email_code_hash):
        conn.execute(
            "UPDATE users SET email_code_attempts = email_code_attempts + 1, email_failures = email_failures + 1 WHERE id = ?",
            (user.id,),
        )
        if fresh.email_failures + 1 >= MAX_FAILURES:
            conn.execute("UPDATE users SET email_locked_until = ? WHERE id = ?", ((datetime.now(UTC) + LOCKOUT).isoformat(), user.id))
            clear()
            return "locked"
        if fresh.email_code_attempts + 1 >= MAX_ATTEMPTS:
            clear()
            return "too_many"
        return "wrong"
    holder = by_email(conn, fresh.email_pending)
    if holder and holder.id != user.id:
        clear()
        return "in_use"
    address = fresh.email_pending
    conn.execute(
        "UPDATE users SET email = ?, email_pending = NULL, email_code_hash = NULL, email_code_expires = NULL, "
        "email_code_attempts = 0, email_failures = 0, "
        "status = CASE WHEN status = 'pending' THEN 'active' ELSE status END WHERE id = ?",
        (address, user.id),
    )
    return "ok"


# --- Email-only members: join code in the subject, then prove the mailbox -----------------------

def pending_by_address(conn: sqlite3.Connection, address: str) -> User | None:
    return one(User, conn.execute(
        "SELECT * FROM users WHERE status = 'pending' AND email IS NULL AND email_pending = ?", (address.strip().lower(),)
    ))


def start_email_only_join(conn: sqlite3.Connection, code: str, address: str, name: str = "") -> str | None:
    """Join-code email from an unknown address: create a *pending* member and return a confirmation code
    to send to that address. The member becomes active only when a reply carries the code, which a
    spoofer of the From address never sees. None if the join code is wrong; raises RateLimited."""
    if not _matches(code, settings.join_code.get_secret_value()):
        return None
    address = address.strip().lower()
    if not EMAIL.match(address) or by_email(conn, address):
        return None
    user = pending_by_address(conn, address)
    if user is None:
        cur = conn.execute(
            "INSERT INTO users (chat_id, email, email_pending, role, name, calendar_token, joined_at, status) "
            "VALUES (NULL, NULL, ?, 'member', ?, ?, ?, 'pending')",
            (address, name[:64], _new_token(), now()),
        )
        user = by_id(conn, cur.lastrowid)
    assert user
    return _issue_code(conn, user, address)


CODE_IN_TEXT = re.compile(r"\b(\d{6})\b")


def confirm_email_only(conn: sqlite3.Connection, address: str, text: str) -> VerifyResult | None:
    """A reply from a pending address: verify the first 6-digit code in it. None if nothing pending."""
    user = pending_by_address(conn, address)
    if user is None:
        return None
    match = CODE_IN_TEXT.search(text)
    return verify_email(conn, user, match.group(1)) if match else "wrong"

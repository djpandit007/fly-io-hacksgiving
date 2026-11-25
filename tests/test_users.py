import pytest

from app import db, users
from app.config import settings


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "users.db")


def test_owner_claim_once(conn):
    owner = users.claim_owner(conn, 1, "owner-code", name="Dhananjay")
    assert owner and owner.is_owner and owner.chat_id == 1 and len(owner.calendar_token) == 24
    assert users.claim_owner(conn, 2, "owner-code") is None  # already owned
    assert users.owner(conn).chat_id == 1


def test_wrong_owner_code(conn):
    assert users.claim_owner(conn, 1, "nope") is None and users.owner(conn) is None


def test_configured_owner_takes_precedence(conn, monkeypatch):
    users.claim_owner(conn, 1, "owner-code")
    monkeypatch.setattr(users, "settings", settings.model_copy(update={"owner_chat_id": 9}))
    assert users.owner(conn).chat_id == 9
    assert users.by_chat(conn, 1).role == "member"


def test_join_wrong_code_and_unconfigured(conn, monkeypatch):
    assert users.join_member(conn, "wrong", chat_id=5) is None
    monkeypatch.setattr(users, "settings", settings.model_copy(update={"join_code": settings.join_code.__class__("")}))
    assert users.join_member(conn, "", chat_id=5) is None and users.join_member(conn, "anything", chat_id=5) is None


def test_join_is_idempotent_and_tokens_are_unique(conn):
    a = users.join_member(conn, "join-code", chat_id=5, name="A")
    again = users.join_member(conn, "join-code", chat_id=5)
    b = users.join_member(conn, "join-code", email="Judge@Example.com")
    assert a.id == again.id and a.calendar_token == again.calendar_token
    assert b.email == "judge@example.com" and b.chat_id is None and b.calendar_token != a.calendar_token
    assert users.by_calendar_token(conn, a.calendar_token).id == a.id


def test_removed_member_and_owner_cannot_be_removed(conn):
    owner = users.claim_owner(conn, 1, "owner-code")
    m = users.join_member(conn, "join-code", chat_id=5)
    assert users.remove(conn, m.id) and users.by_chat(conn, 5).status == "removed"
    assert users.active(users.by_chat(conn, 5)) is None
    assert users.by_calendar_token(conn, m.calendar_token) is None
    assert not users.remove(conn, owner.id)
    assert users.join_member(conn, "join-code", chat_id=5).status == "active"  # can rejoin


def test_legacy_data_is_adopted_by_owner(conn):
    conn.execute("INSERT INTO services (name, terms_url, added_at) VALUES ('Acme', 'https://acme.test', ?)", (db.now(),))
    conn.execute("INSERT INTO ledger (user_id, title, due_on, kind, created_at) VALUES (NULL, 'T', '2030-01-01', 'other', ?)", (db.now(),))
    db.set_kv(conn, "owner_chat", "77")
    owner = users.owner(conn)  # adopts the pre-v2 owner chat
    assert owner.chat_id == 77 and owner.is_owner
    assert conn.execute("SELECT user_id FROM subscriptions").fetchone()[0] == owner.id
    assert conn.execute("SELECT user_id FROM ledger").fetchone()[0] == owner.id
    assert db.get_kv(conn, "owner_chat") is None


def test_legacy_calendar_token_maps_to_owner(conn, monkeypatch):
    owner = users.claim_owner(conn, 1, "owner-code")
    monkeypatch.setattr(users, "settings", settings.model_copy(update={"calendar_token": settings.calendar_token.__class__("legacy")}))
    assert users.by_calendar_token(conn, "legacy").id == owner.id
    assert users.by_calendar_token(conn, "unknown") is None

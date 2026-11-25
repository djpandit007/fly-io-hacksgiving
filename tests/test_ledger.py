from datetime import date, timedelta

import pytest

from app import db, ledger, users


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


@pytest.fixture
def two(conn):
    a = users.join_member(conn, "join-code", chat_id=1, name="A")
    b = users.join_member(conn, "join-code", chat_id=2, name="B")
    return a, b


def test_add_is_idempotent_per_member_and_typed(conn, two):
    a, b = two
    due = date.today() + timedelta(days=2)
    assert ledger.add(conn, a.id, "Opt out: Acme", due, "opt_out")
    assert not ledger.add(conn, a.id, "Opt out: Acme", due, "opt_out")  # same member: deduplicated
    assert ledger.add(conn, b.id, "Opt out: Acme", due, "opt_out")      # other member: their own copy
    [entry] = ledger.upcoming(conn, a.id)
    assert entry.due_on == due and entry.kind == "opt_out" and entry.user_id == a.id


def test_members_only_see_their_own_deadlines(conn, two):
    a, b = two
    ledger.add(conn, a.id, "A's lease notice", date.today() + timedelta(days=30), "notice")
    assert [e.title for e in ledger.upcoming(conn, a.id)] == ["A's lease notice"]
    assert ledger.upcoming(conn, b.id) == []


def test_nudges_cover_all_active_members_once(conn, two):
    a, b = two
    ledger.add(conn, a.id, "soon A", date.today() + timedelta(days=1), "trial")
    ledger.add(conn, b.id, "soon B", date.today() + timedelta(days=2), "trial")
    ledger.add(conn, a.id, "later", date.today() + timedelta(days=30), "renewal")
    due = ledger.due_for_nudge(conn)
    assert {(e.user_id, e.title) for e in due} == {(a.id, "soon A"), (b.id, "soon B")}
    ledger.mark_nudged(conn, [e.id for e in due])
    assert ledger.due_for_nudge(conn) == []


def test_removed_members_are_not_nudged(conn, two):
    a, _ = two
    ledger.add(conn, a.id, "soon A", date.today() + timedelta(days=1), "trial")
    users.remove(conn, a.id)
    assert ledger.due_for_nudge(conn) == []


def test_ics_is_valid_shape(conn, two):
    a, _ = two
    ledger.add(conn, a.id, "Give notice, lease; 12 Elm St", date(2030, 1, 31), "notice", detail="x" * 200)
    ics = ledger.to_ics(ledger.upcoming(conn, a.id, today=date(2029, 12, 1)))
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "DTSTART;VALUE=DATE:20300131" in ics
    assert r"SUMMARY:Give notice\, lease\; 12 Elm St" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))

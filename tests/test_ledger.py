from datetime import date, timedelta

from app import db, ledger


def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def test_add_is_idempotent_and_upcoming_is_typed(tmp_path):
    c = conn(tmp_path)
    due = date.today() + timedelta(days=2)
    assert ledger.add(c, "Opt out: Acme", due, "opt_out")
    assert not ledger.add(c, "Opt out: Acme", due, "opt_out")
    [entry] = ledger.upcoming(c)
    assert entry.due_on == due and entry.kind == "opt_out"


def test_nudge_window_and_marking(tmp_path):
    c = conn(tmp_path)
    ledger.add(c, "soon", date.today() + timedelta(days=1), "trial")
    ledger.add(c, "later", date.today() + timedelta(days=30), "renewal")
    due = ledger.due_for_nudge(c)
    assert [e.title for e in due] == ["soon"]
    ledger.mark_nudged(c, [e.id for e in due])
    assert ledger.due_for_nudge(c) == []


def test_ics_is_valid_shape(tmp_path):
    c = conn(tmp_path)
    ledger.add(c, "Give notice, lease; 12 Elm St", date(2030, 1, 31), "notice", detail="x" * 200)
    ics = ledger.to_ics(ledger.upcoming(c, today=date(2029, 12, 1)))
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "DTSTART;VALUE=DATE:20300131" in ics
    assert r"SUMMARY:Give notice\, lease\; 12 Elm St" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))

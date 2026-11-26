"""Answer feedback: tokens, one vote per answer, reasons only after 👎, retention, and metadata only."""

from datetime import UTC, datetime, timedelta

import pytest

from app import db, feedback, taps
from app.outgoing import Answer

VERDICT = Answer(kind="verdict", model="deepseek-v4.1-flash", prompt_version="v2", quotes_ok=True)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "fb.db")


def row(conn, token):
    return conn.execute("SELECT * FROM answers WHERE token = ?", (token,)).fetchone()


def test_issue_records_metadata_and_an_unguessable_token(conn):
    token = feedback.issue(conn, VERDICT)
    assert taps.TOKEN.fullmatch(token) and token != feedback.issue(conn, VERDICT)
    r = row(conn, token)
    assert (r["kind"], r["model"], r["prompt_version"], r["quotes_ok"], r["vote"]) == (
        "verdict", "deepseek-v4.1-flash", "v2", 1, None)


def test_last_vote_wins(conn):
    token = feedback.issue(conn, VERDICT)
    assert feedback.vote(conn, token, "up")
    assert feedback.vote(conn, token, "down")
    assert row(conn, token)["vote"] == "down"
    assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 1


def test_reason_only_after_a_thumbs_down(conn):
    token = feedback.issue(conn, VERDICT)
    feedback.vote(conn, token, "up")
    assert not feedback.reason(conn, token, "unclear")
    feedback.vote(conn, token, "down")
    assert feedback.reason(conn, token, "unclear")
    assert row(conn, token)["reason"] == "unclear"
    feedback.vote(conn, token, "up")  # changing to 👍 clears the reason
    assert row(conn, token)["reason"] is None


def test_unknown_token_is_refused(conn):
    assert not feedback.vote(conn, "AAAAAAAAAAAA", "up")
    assert not feedback.reason(conn, "AAAAAAAAAAAA", "wrong")
    assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0


def test_prune_after_90_days(conn):
    old, new = feedback.issue(conn, VERDICT), feedback.issue(conn, VERDICT)
    conn.execute("UPDATE answers SET sent_at = ? WHERE token = ?",
                 ((datetime.now(UTC) - timedelta(days=91)).isoformat(), old))
    feedback.prune(conn)
    assert row(conn, old) is None and row(conn, new) is not None


def test_issue_fails_safely(tmp_path):
    conn = db.connect(tmp_path / "ro.db")
    conn.execute("DROP TABLE answers")
    assert feedback.issue(conn, VERDICT) is None
    assert feedback.with_rating(conn, [[("💡 x", "v1:fx:arbitration")]], VERDICT) == [[("💡 x", "v1:fx:arbitration")]]


def test_with_rating_adds_its_own_row(conn):
    rows = feedback.with_rating(conn, [[("🗑 Unwatch A", "v1:u:1")]], VERDICT)
    assert rows[0] == [("🗑 Unwatch A", "v1:u:1")]
    assert [label for label, _ in rows[1]] == ["👍", "👎"]
    assert feedback.with_rating(conn, [], None) == []


def test_nothing_but_metadata_is_stored(conn):
    alert = Answer(kind="alert", model="m", prompt_version="v1", change_id=17)
    feedback.issue(conn, alert)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(answers)")}
    assert not columns & {"text", "url", "user_id", "chat_id", "member"}


def test_summary_per_kind_reason_and_prompt_version(conn):
    for version, votes in (("v1", ["up", "down"]), ("v2", ["up", "up", "up", "down"])):
        for v in votes:
            t = feedback.issue(conn, VERDICT.model_copy(update={"prompt_version": version}))
            feedback.vote(conn, t, v)
            if v == "down":
                feedback.reason(conn, t, "unclear")
    t = feedback.issue(conn, Answer(kind="alert", model="m", prompt_version="v1", change_id=3))
    feedback.vote(conn, t, "up")
    feedback.issue(conn, VERDICT)  # sent but never rated: not counted
    verdicts, alerts = feedback.summary(conn, 7)
    assert verdicts == "• Verdict cards — 6 rated, 66% 👍; 👎 because: Unclear 2 (by prompt: v1 50% of 2, v2 75% of 4)"
    assert alerts == "• Change alerts — 1 rated, 100% 👍 (by prompt: v1 100% of 1)"


def test_summary_is_empty_without_votes(conn):
    feedback.issue(conn, VERDICT)
    assert feedback.summary(conn, 7) == []


def test_issued_tokens_always_fit_the_tap_grammar(conn):
    for _ in range(50):
        assert taps.parse(taps.build("fb", feedback.issue(conn, VERDICT), "u"))

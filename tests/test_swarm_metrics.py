"""Swarm and counter-measure telemetry: sanitized, pruned, and summarised in /stats."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app import db, metrics
from tests.test_swarm import SCOUT, URL, framed, make, mock_api, ok_result

SECRETS = ("acme.test", "https://", "/terms", "contract.pdf", "member@example.com", "user_id")


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "metrics.db")


def stored(conn) -> str:
    out = []
    for table in ("swarm_events", "metric_counts"):
        out += [" ".join(str(v) for v in tuple(r)) for r in conn.execute(f"SELECT * FROM {table}")]
    return "\n".join(out)


def test_labels_keep_only_generic_words():
    assert metrics.label("playwright_crawl") == "playwright_crawl"
    assert metrics.label("HTTPStatusError") == "httpstatus_error"
    for leaky in ("https://acme.test/terms", "contract.pdf", "member@example.com", "user 42", "x" * 80):
        assert metrics.label(leaky) == "other"


def test_unknown_names_are_never_written(conn):
    metrics.swarm_event(conn, "scout", "https://acme.test/terms")
    metrics.swarm_event(conn, "member-42", "run")
    metrics.swarm_count(conn, "scout", "https://acme.test")
    metrics.countermeasure(conn, "member@example.com")
    metrics.countermeasure(conn, "opt_out", "sent to member@example.com")
    metrics.swarm_event(conn, "advocate", "run", "redline for contract.pdf")
    text = stored(conn)
    assert not any(s in text for s in SECRETS)
    assert conn.execute("SELECT outcome FROM metric_counts WHERE name = 'opt_out'").fetchone()["outcome"] == "error"
    assert conn.execute("SELECT detail FROM swarm_events").fetchone()["detail"] == "other"


@respx.mock
async def test_a_scout_crawl_stores_no_url_or_member(conn, monkeypatch):
    from app import swarm

    async def public(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(swarm, "resolve_public", public)
    mock_api(httpx.Response(200, content=framed(ok_result())))
    respx.post(f"{SCOUT}/exec").mock(side_effect=httpx.ConnectError(f"cannot reach {URL}"))
    sw = make(conn)
    with pytest.raises(httpx.ConnectError):
        await sw.scout(URL)
    mock_api(httpx.Response(200, content=framed(ok_result())))
    await sw.scout(URL)
    await sw.aclose()
    text = stored(conn)
    assert "scout_job" in text and "checkpoint_restore" in text
    assert not any(s in text for s in SECRETS)


def test_report_shows_swarm_and_countermeasures(conn):
    metrics.swarm_count(conn, "scout", "job", "ok", 900)
    metrics.swarm_count(conn, "scout", "job", "timeout", 45000)
    metrics.swarm_count(conn, "scout", "egress_lock", "ok", 40)
    metrics.swarm_count(conn, "scout", "restore", "ok", 300)
    metrics.swarm_count(conn, "scout", "restore", "ok", 500)
    metrics.countermeasure(conn, "redline_kit", "ok")
    metrics.countermeasure(conn, "opt_out", "ok")
    metrics.countermeasure(conn, "opt_out", "error")
    report = metrics.report(conn)
    assert "<b>Swarm</b>" in report
    assert "Scout: 2 runs (1 failed), 1 DNS egress locks, 2 checkpoint restores, avg 400 ms, max 500 ms" in report
    assert "Counter-measures: 1 redline kits, 2 opt-out dispatches (1 failed)" in report


def test_report_without_swarm_activity(conn):
    assert "No Scout or Advocate activity." in metrics.report(conn)


def test_prune_drops_old_swarm_events(conn):
    old = (datetime.now(UTC) - timedelta(days=91)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO swarm_events (at, sprite_role, event) VALUES (?, 'scout', 'run')", (old,))
    metrics.swarm_event(conn, "scout", "run", "playwright_crawl")
    metrics.prune(conn)
    assert [r["detail"] for r in conn.execute("SELECT detail FROM swarm_events")] == ["playwright_crawl"]

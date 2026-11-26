"""Member-scoped lookups used by the researcher: a member only ever sees their own watchlist."""

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from app import ask_tools, db, users
from app.ask_tools import AskDeps, Clause

SPOTIFY_TEXT = (
    "## Payments\nYou pay the subscription price each month and we may change the price with notice.\n"
    "## Disputes\nAny dispute is settled by binding arbitration rather than in a court of law.\n"
    "## Other\nFirst other section says we keep logs for ninety days.\n"
    "## Other\nSecond other section says we may close idle accounts.\n"
)
NOTION_TEXT = "## Payments\nNotion bills the workspace owner on the first day of every billing cycle.\n"

DISPUTE_QUOTE = "Any dispute is settled by binding arbitration"
NOTION_CHANGE_QUOTE = "Notion will now charge a fee for guest seats"


def _ts(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _service(conn, name, text=None, taken_days_ago=10):
    sid = conn.execute(
        "INSERT INTO services (name, terms_url, added_at) VALUES (?, 'https://x.test', ?)", (name, _ts(100))
    ).lastrowid
    snap = None
    if text is not None:
        snap = conn.execute(
            "INSERT INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (?, ?, 'live', ?, ?)",
            (sid, _ts(taken_days_ago), f"sha-{name}", text),
        ).lastrowid
    return sid, snap


def _change(conn, sid, snap, heading, before, after, material=0.9, summary="Summary", days_ago=5):
    return conn.execute(
        """INSERT INTO changes (service_id, from_snap, to_snap, detected_at, effective_at, heading, kind,
                                before_text, after_text, material, summary)
           VALUES (?, NULL, ?, ?, ?, ?, 'modified', ?, ?, ?, ?)""",
        (sid, snap, _ts(days_ago), _ts(days_ago), heading, before, after, material, summary),
    ).lastrowid


@pytest.fixture
def world(tmp_path):
    conn = db.connect(tmp_path / "a.db")
    m1 = users.join_member(conn, "join-code", chat_id=1, name="A")
    m2 = users.join_member(conn, "join-code", chat_id=2, name="B")
    spotify, spotify_snap = _service(conn, "Spotify", SPOTIFY_TEXT)
    notion, notion_snap = _service(conn, "Notion", NOTION_TEXT)
    conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (m1.id, spotify, _ts(1)))
    conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (m2.id, notion, _ts(1)))
    return {
        "conn": conn, "mine": AskDeps(conn, m1.id), "theirs": AskDeps(conn, m2.id),
        "spotify": spotify, "spotify_snap": spotify_snap, "notion": notion, "notion_snap": notion_snap,
    }


def test_list_services_is_only_mine(world):
    assert ask_tools.list_services(world["mine"]) == ["Spotify"]
    assert ask_tools.list_services(world["theirs"]) == ["Notion"]


def test_list_sections_of_unwatched_service_is_empty(world):
    deps = world["mine"]
    _change(world["conn"], world["notion"], world["notion_snap"], "Payments", "old", "new")
    assert ask_tools.list_sections(deps, "Notion") == []
    assert ask_tools.read_section(deps, "Notion", "Payments") == ""
    assert ask_tools.recent_changes(deps, "Notion") == []
    assert ask_tools.list_sections(deps, "Nonexistent") == []


def test_service_name_is_case_insensitive(world):
    assert ask_tools.list_sections(world["mine"], "spotify") == ["Payments", "Disputes", "Other", "Other"]


def test_read_section_joins_duplicate_headings(world):
    body = ask_tools.read_section(world["mine"], "Spotify", " other ")
    assert "First other section" in body and "Second other section" in body


def test_service_without_snapshot(world):
    conn = world["conn"]
    sid, _ = _service(conn, "Empty")
    conn.execute(
        "INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)",
        (world["mine"].user_id, sid, _ts(1)),
    )
    assert ask_tools.list_sections(world["mine"], "Empty") == []
    assert ask_tools.read_section(world["mine"], "Empty", "Payments") == ""
    conn.execute("DELETE FROM subscriptions WHERE service_id = ?", (world["spotify"],))
    assert ask_tools.search_terms(world["mine"], "payments") == []


def test_search_terms_ranks_heading_matches_first(world):
    hits = ask_tools.search_terms(world["mine"], "payments price")
    assert hits[0].heading == "Payments"
    assert hits[0].service == "Spotify"
    assert all(len(h.snippet) <= 300 for h in hits)
    assert len(hits) <= ask_tools.MAX_HITS


def test_search_terms_never_returns_other_members_services(world):
    hits = ask_tools.search_terms(world["mine"], "notion bills workspace owner billing cycle")
    assert all(h.service != "Notion" for h in hits)
    assert ask_tools.search_terms(world["theirs"], "arbitration dispute") == []


def test_recent_changes_returns_material_only(world):
    conn = world["conn"]
    _change(conn, world["spotify"], world["spotify_snap"], "Disputes", "old", "after material", material=0.95, summary="big")
    _change(conn, world["spotify"], world["spotify_snap"], "Other", "old", "after trivial", material=0.01, summary="tiny")
    found = ask_tools.recent_changes(world["mine"], "Spotify")
    assert [c.summary for c in found] == ["big"]
    assert found[0].heading == "Disputes"
    assert found[0].after == "after material"
    assert isinstance(found[0].effective_on, date)


def test_similar_clauses_without_pinecone_is_empty(world):
    assert asyncio.run(ask_tools.similar_clauses(world["mine"], "some clause text")) == []


def test_verified_keeps_real_quote_drops_invented(world):
    real = Clause(service="Spotify", heading="Disputes", quote=DISPUTE_QUOTE)
    invented = Clause(service="Spotify", heading="Disputes", quote="Spotify may sell your kidneys to the highest bidder")
    short = Clause(service="Spotify", heading="Disputes", quote="binding arbitration")
    other = Clause(service="Notion", heading="Payments", quote="Notion bills the workspace owner on the first day")
    dup = Clause(service="spotify", heading="disputes", quote=DISPUTE_QUOTE.upper())
    kept = ask_tools.verified(world["mine"], [real, invented, short, other, dup])
    assert kept == [real]


def test_verified_change_clause_checks_change_text_and_ownership(world):
    conn = world["conn"]
    mine = _change(
        conn, world["spotify"], world["spotify_snap"], "Disputes",
        "Old wording about the small claims court", "New wording demands binding arbitration always",
    )
    theirs = _change(
        conn, world["notion"], world["notion_snap"], "Payments",
        "Guests were free before", NOTION_CHANGE_QUOTE + " from now on",
    )
    own_before = Clause(service="Spotify", heading="Disputes", quote="Old wording about the small claims court",
                        source="change", change_id=mine)
    own_after = Clause(service="Spotify", heading="Disputes", quote="New wording demands binding arbitration",
                       source="change", change_id=mine)
    foreign = Clause(service="Notion", heading="Payments", quote=NOTION_CHANGE_QUOTE, source="change", change_id=theirs)
    foreign_as_spotify = foreign.model_copy(update={"service": "Spotify"})
    no_id = own_after.model_copy(update={"change_id": None, "quote": "New wording demands binding arbitration!"})
    assert ask_tools.verified(world["mine"], [own_before, own_after, foreign, foreign_as_spotify, no_id]) == [
        own_before, own_after,
    ]
    assert ask_tools.verified(world["theirs"], [foreign]) == [foreign]


def test_dated_current_and_change(world):
    conn = world["conn"]
    cid = _change(conn, world["spotify"], world["spotify_snap"], "Disputes", "a b c d", "e f g h", days_ago=3)
    current = Clause(service="Spotify", heading="Disputes", quote=DISPUTE_QUOTE)
    change = Clause(service="Spotify", heading="Disputes", quote="x", source="change", change_id=cid)
    assert ask_tools.dated(world["mine"], current) == (datetime.now(UTC) - timedelta(days=10)).date()
    assert ask_tools.dated(world["mine"], change) == (datetime.now(UTC) - timedelta(days=3)).date()
    assert ask_tools.dated(world["theirs"], current) is None


def _watch_notion_too(world):
    world["conn"].execute(
        "INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)",
        (world["mine"].user_id, world["notion"], _ts(1)),
    )


def test_verified_change_clause_takes_service_and_heading_from_the_change(world):
    _watch_notion_too(world)
    cid = _change(
        world["conn"], world["notion"], world["notion_snap"], "Guest seats",
        "Guests were free before", NOTION_CHANGE_QUOTE + " from now on",
    )
    mislabelled = Clause(
        service="Spotify", heading="Disputes", quote=NOTION_CHANGE_QUOTE, source="change", change_id=cid
    )
    [kept] = ask_tools.verified(world["mine"], [mislabelled])
    assert (kept.service, kept.heading) == ("Notion", "Guest seats")
    assert kept.quote == NOTION_CHANGE_QUOTE and kept.change_id == cid


def test_verified_returns_canonical_service_name(world):
    [kept] = ask_tools.verified(
        world["mine"], [Clause(service=" spotify ", heading="Disputes", quote=DISPUTE_QUOTE)]
    )
    assert kept.service == "Spotify"


def test_verified_dedupes_after_canonicalising(world):
    clauses = [
        Clause(service=" Spotify", heading="Disputes", quote=DISPUTE_QUOTE),
        Clause(service="Spotify", heading="Disputes", quote=DISPUTE_QUOTE),
    ]
    assert len(ask_tools.verified(world["mine"], clauses)) == 1

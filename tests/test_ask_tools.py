"""Member-scoped lookups used by the researcher: a member only ever sees their own watchlist."""

import asyncio
import logging
import re
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from app import ask_tools, db, users
from app.ask_tools import AskDeps, Clause
from app.config import settings
from app.gateway import Gateway

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


def _now_says(conn, sid, text, days_ago=2):
    """A newer snapshot of the service holding `text`, as after a change to it."""
    conn.execute(
        "INSERT INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (?, ?, 'live', ?, ?)",
        (sid, _ts(days_ago), f"sha-{sid}-{days_ago}-{len(text)}", text),
    )


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


EMBED = "https://pinecone.test/embed"
QUERY = "https://pinecone.test/indexes/fine-print/query"


@pytest.mark.parametrize("failing", ["embed", "query"])
@respx.mock
async def test_similar_clauses_error_body_is_not_logged(world, caplog, failing):
    caplog.set_level(logging.DEBUG)
    echo = httpx.Response(400, json={"error": "bad input: zebra-quote text"})
    embedded = httpx.Response(200, json={"data": [{"values": [0.1] * 4}], "usage": {}})
    respx.post(EMBED).mock(return_value=echo if failing == "embed" else embedded)
    respx.post(QUERY).mock(return_value=echo)
    async with Gateway(settings.pinecone) as gw:
        deps = AskDeps(world["conn"], world["mine"].user_id, gw)
        assert await ask_tools.similar_clauses(deps, "zebra-quote text") == []
    assert "zebra-quote" not in caplog.text
    assert "-> 400" in caplog.text


def test_verified_keeps_real_quote_drops_invented(world):
    real = Clause(service="Spotify", heading="Disputes", quote=DISPUTE_QUOTE)
    invented = Clause(service="Spotify", heading="Disputes", quote="Spotify may sell your kidneys to the highest bidder")
    short = Clause(service="Spotify", heading="Disputes", quote="binding arbitration")
    other = Clause(service="Notion", heading="Payments", quote="Notion bills the workspace owner on the first day")
    dup = Clause(service="spotify", heading="disputes", quote=DISPUTE_QUOTE.upper())
    kept = ask_tools.verified(world["mine"], [real, invented, short, other, dup])
    assert kept == [real]


def test_verified_quote_must_sit_inside_one_of_the_same_named_sections(world):
    within = Clause(service="Spotify", heading="Other", quote="Second other section says we may close idle accounts")
    across = Clause(service="Spotify", heading="Other",
                    quote="we keep logs for ninety days. Second other section says")
    assert ask_tools.verified(world["mine"], [within, across]) == [within]


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
    _now_says(conn, world["spotify"], SPOTIFY_TEXT + "## Disputes\nNew wording demands binding arbitration always\n")
    _now_says(conn, world["notion"], NOTION_TEXT + "## Payments\n" + NOTION_CHANGE_QUOTE + " from now on\n")
    own_before = Clause(service="Spotify", heading="Disputes", quote="Old wording about the small claims court",
                        source="change", change_id=mine)
    own_after = Clause(service="Spotify", heading="Disputes", quote="New wording demands binding arbitration",
                       source="change", change_id=mine)
    foreign = Clause(service="Notion", heading="Payments", quote=NOTION_CHANGE_QUOTE, source="change", change_id=theirs)
    foreign_as_spotify = foreign.model_copy(update={"service": "Spotify"})
    no_id = own_after.model_copy(update={"change_id": None, "quote": "New wording demands binding arbitration!"})
    # Removed wording is not current terms: only the change's new text can be quoted.
    assert ask_tools.verified(world["mine"], [own_before, own_after, foreign, foreign_as_spotify, no_id]) == [
        own_after,
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
    _now_says(world["conn"], world["notion"], NOTION_TEXT + "## Guest seats\n" + NOTION_CHANGE_QUOTE + " from now on\n")
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


def test_change_clause_quote_spanning_old_and_new_text_is_dropped(world):
    cid = _change(
        world["conn"], world["spotify"], world["spotify_snap"], "Disputes",
        "Old wording about the small claims court", "New wording demands binding arbitration always",
    )
    spanning = Clause(service="Spotify", heading="Disputes", quote="about the small claims court New wording demands",
                      source="change", change_id=cid)
    assert ask_tools.verified(world["mine"], [spanning]) == []


def test_change_clause_wording_dropped_by_a_later_version_is_dropped(world):
    conn = world["conn"]
    cid = _change(
        conn, world["spotify"], world["spotify_snap"], "Refunds",
        "No refunds", "Refunds are given within fourteen days of purchase", days_ago=5,
    )
    clause = Clause(service="Spotify", heading="Refunds", quote="Refunds are given within fourteen days",
                    source="change", change_id=cid)
    _now_says(conn, world["spotify"], SPOTIFY_TEXT + "## Refunds\nRefunds are given within fourteen days of purchase\n",
              days_ago=4)
    assert ask_tools.verified(world["mine"], [clause]) == [clause]
    _now_says(conn, world["spotify"], SPOTIFY_TEXT + "## Refunds\nNo refunds are given.\n", days_ago=1)
    assert ask_tools.verified(world["mine"], [clause]) == []


# --- Long sections, snippets and change lists --------------------------------------------------------

def _watch(world, name, text):
    sid, _ = _service(world["conn"], name, text)
    world["conn"].execute(
        "INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)",
        (world["mine"].user_id, sid, _ts(1)),
    )
    return sid


def _paragraph(i: int) -> str:
    return f"Paragraph {i} says " + " ".join(f"w{i}x{j}" for j in range(80)) + "."


def _parts(deps, service, heading):
    first = ask_tools.read_section(deps, service, heading)
    total = int(re.match(r"\[part 1 of (\d+)\]\n", first).group(1))
    return [ask_tools.read_section(deps, service, heading, part=n) for n in range(1, total + 1)]


def test_long_section_is_readable_in_parts(world):
    paragraphs = [_paragraph(i) for i in range(40)]
    body = "\n\n".join(paragraphs)
    assert len(body) > 20_000
    _watch(world, "Longform", "## Long\n" + body + "\n")
    parts = _parts(world["mine"], "Longform", "Long")
    assert len(parts) >= 3
    for n, part in enumerate(parts, 1):
        header, text = part.split("\n", 1)
        assert header == f"[part {n} of {len(parts)}]"
        assert len(text) <= ask_tools.MAX_SECTION_CHARS
    joined = "\n".join(parts)
    assert all(p in joined for p in paragraphs)  # broken on paragraph boundaries: none is split
    assert ask_tools.read_section(world["mine"], "Longform", "Long", part=len(parts) + 1) == ""
    assert ask_tools.read_section(world["mine"], "Longform", "Long", part=0) == ""


def test_long_paragraph_without_breaks_is_still_fully_readable(world):
    body = " ".join(f"word{n}" for n in range(4000))
    assert len(body) > 20_000
    _watch(world, "Wall", "## Wall\n" + body + "\n")
    texts = [p.split("\n", 1)[1] for p in _parts(world["mine"], "Wall", "Wall")]
    assert all(len(t) <= ask_tools.MAX_SECTION_CHARS for t in texts)
    assert "".join(texts) == body


def test_short_section_has_no_part_header(world):
    assert ask_tools.read_section(world["mine"], "Spotify", "Disputes").startswith("Any dispute")
    assert ask_tools.read_section(world["mine"], "Spotify", "Disputes", part=2) == ""


def test_quote_from_the_last_part_verifies(world):
    paragraphs = [_paragraph(i) for i in range(40)]
    _watch(world, "Longform", "## Long\n" + "\n\n".join(paragraphs) + "\n")
    quote = paragraphs[-1][:120]
    [kept] = ask_tools.verified(world["mine"], [Clause(service="Longform", heading="Long", quote=quote)])
    assert kept.quote == quote


def test_snippet_is_centred_on_the_matching_word(world):
    _watch(world, "Deep", "## Misc\n" + "filler " * 100 + "the zebrafish clause applies here. " + "tail " * 100 + "\n")
    [hit] = ask_tools.search_terms(world["mine"], "zebrafish")
    assert "zebrafish" in hit.snippet
    assert len(hit.snippet) <= ask_tools.SNIPPET_CHARS


def test_snippet_falls_back_to_the_start_on_a_heading_match(world):
    _watch(world, "Deep", "## Zebrafish\nAlpha beta gamma. " + "filler " * 100 + "\n")
    [hit] = ask_tools.search_terms(world["mine"], "zebrafish")
    assert hit.snippet.startswith("Alpha beta gamma.")


def test_recent_changes_are_the_latest_ten_and_cut(world):
    for days_ago in range(1, 13):
        _change(world["conn"], world["spotify"], world["spotify_snap"], "Disputes", "old",
                f"change from {days_ago} days ago " + "x" * 3000, days_ago=days_ago)
    found = ask_tools.recent_changes(world["mine"], "Spotify")
    assert len(found) == ask_tools.MAX_CHANGES == 10
    assert all(len(c.after) <= ask_tools.MAX_CHANGE_CHARS == 2000 for c in found)
    ages = {int(re.match(r"change from (\d+) days", c.after).group(1)) for c in found}
    assert ages == set(range(1, 11))

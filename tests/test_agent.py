"""Multi-user workflow with OpenCode (Jev + chat), Pinecone and the history backfill mocked."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from app import agent, db, ledger, users
from app.judge import Verdict
from app.watch import html_to_markdown, normalise
from tests.test_judge import GOOD
from tests.test_watch import NEW_HTML, OLD_HTML

JEV = "https://opencode.test/inference/systemone/v1/systemone"
CHAT = "https://opencode.test/inference/openai/v1/chat/completions"
EMBED = "https://pinecone.test/embed"
QUERY = "https://pinecone.test/indexes/fine-print/query"
UPSERT = "https://pinecone.test/indexes/fine-print/vectors/upsert"
SUMMARY = "Disputes now go to arbitration; opt out within 30 days."


def text(html):
    return normalise(html_to_markdown(html))


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "a.db")


@pytest.fixture
def members(conn):
    return [users.join_member(conn, "join-code", chat_id=i, name=n) for i, n in ((1, "A"), (2, "B"), (3, "C"))]


def mock_providers(material=0.91):
    answers = {**GOOD, "answers": {**GOOD["answers"], "material": {"type": "noul", "noul": material}}}
    return {
        "jev": respx.post(JEV).mock(return_value=httpx.Response(200, json=answers)),
        "chat": respx.post(CHAT).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": SUMMARY}}]})),
        "embed": respx.post(EMBED).mock(return_value=httpx.Response(200, json={"data": [{"values": [0.1] * 1024}]})),
        "query": respx.post(QUERY).mock(return_value=httpx.Response(200, json={"matches": [
            {"id": "chg-9", "score": 0.83, "metadata": {"service": "Spotify", "heading": "Disputes"}},
            {"id": "chg-8", "score": 0.2, "metadata": {"service": "Weak"}},
        ]})),
        "upsert": respx.post(UPSERT).mock(return_value=httpx.Response(200, json={"upsertedCount": 1})),
    }


@pytest.fixture
def history(monkeypatch):
    """Backfill returns OLD then NEW versions; the live page is NEW (unchanged since)."""
    from app.history import Version

    calls = []

    async def fake_backfill(name, url, days=365):
        calls.append(name)
        now = datetime.now(UTC)
        return [Version(taken_at=now - timedelta(days=200), text=text(OLD_HTML), origin="wayback"),
                Version(taken_at=now - timedelta(days=1), text=text(NEW_HTML), origin="wayback")]

    async def fake_page_text(cl, url):
        return text(NEW_HTML)

    async def no_grade(q, client=None):
        return None

    async def no_companions(terms_url, client=None):
        return {}

    monkeypatch.setattr(agent.discover, "discover_companions", no_companions)
    monkeypatch.setattr(agent, "backfill", fake_backfill)
    monkeypatch.setattr(agent, "page_text", fake_page_text)
    monkeypatch.setattr(agent.tosdr, "lookup", no_grade)
    return calls


@respx.mock
async def test_first_subscriber_gets_judged_history_with_their_deadline(conn, members, history):
    m = mock_providers()
    a = members[0]
    cl = agent.Clients()
    _svc, reports = await agent.subscribe(conn, cl, a, "Acme", "https://acme.test/terms")
    await cl.aclose()

    assert {r.heading for r in reports} == {"2. Payments", "3. Arbitration"}
    arb = next(r for r in reports if r.heading == "3. Arbitration")
    assert arb.summary == SUMMARY and arb.verdict.favours == "company"
    assert arb.deadline == (datetime.now(UTC) - timedelta(days=1)).date() + timedelta(days=30)
    assert [e.title for e in ledger.upcoming(conn, a.id)] == ["Opt out: Acme — 3. Arbitration"]
    assert [s.name for s in agent.services_for(conn, a.id)] == ["Acme"]
    assert m["jev"].call_count == 2  # one per changed section
    assert json.loads(m["jev"].calls[0].request.content)["model"] == "jev-1.14"
    assert json.loads(m["upsert"].calls[0].request.content)["vectors"][0]["metadata"]["service"] == "Acme"
    assert not m["query"].called  # A watches nothing else, so there is nothing to compare with


@respx.mock
async def test_second_subscriber_gets_replay_without_new_judging(conn, members, history):
    m = mock_providers()
    a, b, _ = members
    cl = agent.Clients()
    await agent.subscribe(conn, cl, a, "Acme", "https://acme.test/terms")
    judged = m["jev"].call_count
    _, replay = await agent.subscribe(conn, cl, b, "Acme", "https://acme.test/terms")
    await cl.aclose()

    assert m["jev"].call_count == judged and history == ["Acme"]  # no new judging, no second backfill
    assert {r.heading for r in replay} == {"2. Payments", "3. Arbitration"}
    assert [e.title for e in ledger.upcoming(conn, b.id)] == ["Opt out: Acme — 3. Arbitration"]
    assert next(r for r in replay if r.heading == "3. Arbitration").verdict.category == "arbitration"


@respx.mock
async def test_concurrent_first_watches_backfill_once(conn, members, history):
    mock_providers()
    a, b, _ = members
    cl = agent.Clients()
    (_, ra), (_, rb) = await asyncio.gather(
        agent.subscribe(conn, cl, a, "Acme", "https://acme.test/terms"),
        agent.subscribe(conn, cl, b, "Acme", "https://acme.test/terms"),
    )
    await cl.aclose()
    assert history == ["Acme"] and len(ra) == len(rb) == 2


@respx.mock
async def test_precedents_only_from_the_members_own_services(conn, members, history):
    m = mock_providers()
    a, b, _ = members
    cl = agent.Clients()
    agent.add_service(conn, "Spotify", "https://spotify.test/terms")
    spotify = agent.add_service(conn, "Spotify", "https://spotify.test/terms")
    conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (a.id, spotify.id, db.now()))
    _, reports_a = await agent.subscribe(conn, cl, a, "Acme", "https://acme.test/terms")
    m["query"].reset()
    _, reports_b = await agent.subscribe(conn, cl, b, "Acme", "https://acme.test/terms")
    await cl.aclose()

    assert any(p.service == "Spotify" for r in reports_a for p in r.precedents)
    assert all(not r.precedents for r in reports_b)  # B doesn't watch Spotify
    assert not m["query"].called  # B watches only Acme: no query at all


@respx.mock
async def test_precedent_query_filters_to_member_services(conn, members, history):
    m = mock_providers()
    a = members[0]
    cl = agent.Clients()
    for name in ("Spotify", "Uber"):
        svc = agent.add_service(conn, name, f"https://{name.lower()}.test/terms")
        conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (a.id, svc.id, db.now()))
    await agent.subscribe(conn, cl, a, "Acme", "https://acme.test/terms")
    await cl.aclose()
    body = json.loads(m["query"].calls[0].request.content)
    assert body["filter"] == {"service": {"$in": ["Spotify", "Uber"]}}


@respx.mock
async def test_tick_fans_out_one_judgment_to_each_subscriber_only(conn, members, history, monkeypatch):
    m = mock_providers()
    a, b, c = members
    cl = agent.Clients()
    await agent.subscribe(conn, cl, a, "Acme", "https://acme.test/terms")
    await agent.subscribe(conn, cl, b, "Acme", "https://acme.test/terms")
    judged = m["jev"].call_count

    changed = text(NEW_HTML).replace("individual arbitration", "individual arbitration in Texas, with no right to appeal")

    async def page(cl_, url):
        return changed

    monkeypatch.setattr(agent, "page_text", page)
    result = await agent.tick(conn, cl)
    await cl.aclose()

    assert result.checked == 1 and result.changes == 1
    assert m["jev"].call_count == judged + 1  # judged once for two watchers
    assert set(result.deliveries) == {a.id, b.id} and c.id not in result.deliveries
    assert all("Acme" in msgs[0].text for msgs in result.deliveries.values())
    assert all(msgs[0].buttons[0][0][0] == "🗑 Unwatch Acme" for msgs in result.deliveries.values())


@respx.mock
async def test_tick_skips_services_whose_watchers_were_removed(conn, members, history):
    mock_providers()
    a = members[0]
    cl = agent.Clients()
    await agent.subscribe(conn, cl, a, "Acme", "https://acme.test/terms")
    users.remove(conn, a.id)
    result = await agent.tick(conn, cl)
    await cl.aclose()
    assert result.checked == 0 and result.deliveries == {}


@respx.mock
async def test_tick_nudges_each_member_separately(conn, members):
    a, b, _ = members
    ledger.add(conn, a.id, "A trial ends", date.today() + timedelta(days=1), "trial")
    ledger.add(conn, b.id, "B notice", date.today() + timedelta(days=2), "notice")
    cl = agent.Clients()
    result = await agent.tick(conn, cl)
    await cl.aclose()
    assert "A trial ends" in result.deliveries[a.id][0].text and "B notice" not in result.deliveries[a.id][0].text
    assert "B notice" in result.deliveries[b.id][0].text and result.nudges == 2


@respx.mock
async def test_immaterial_change_is_stored_but_not_reported(conn, members, history):
    m = mock_providers(material=0.05)
    cl = agent.Clients()
    _, reports = await agent.subscribe(conn, cl, members[0], "Acme", "https://acme.test/terms")
    await cl.aclose()
    assert reports == [] and not m["chat"].called
    assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 2


@respx.mock
async def test_wrapped_counts_only_the_members_services(conn, members, history):
    mock_providers()
    a, b, _ = members
    cl = agent.Clients()
    await agent.subscribe(conn, cl, a, "Acme", "https://acme.test/terms")
    wa = await agent.wrapped(conn, cl, a)
    wb = await agent.wrapped(conn, cl, b)
    await cl.aclose()
    assert "2 material changes you accepted" in wa.text and wa.rate.kind == "wrapped"
    assert "Suspiciously quiet" in wb.text and wb.rate is None  # nothing written, nothing to rate


@respx.mock
async def test_verdict_card_survives_writer_failure():
    flags = {"model": "jev-1.14", "answers": {k: {"type": "noul", "noul": 0.9} for k in
             ("auto_renews", "arbitration", "data_sharing", "unilateral_changes", "content_licence", "hard_to_cancel")}}
    respx.post(JEV).mock(return_value=httpx.Response(200, json=flags))
    respx.post(CHAT).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": None}, "finish_reason": "length"}]}))
    cl = agent.Clients()
    out = await agent.verdict_card(cl, "Lease", "Some lease text")
    card = out.text
    await cl.aclose()
    assert out.rate is None  # the writer wrote nothing, so there is nothing to rate
    assert "<b>What I checked for</b>" in card and "🔴 Found (90% sure): arbitration" in card
    assert "not legal advice" in card


def test_later_watcher_cannot_repoint_a_shared_service(conn):
    original = agent.add_service(conn, "Youtube", "https://www.youtube.com/t/terms")
    # Same display name from a look-alike domain: a separate service, never the shared one.
    lookalike = agent.add_service(conn, "Youtube", "https://youtube.co/terms")
    # Same URL written differently: the same service, URL unchanged.
    same = agent.add_service(conn, "Youtube", "https://youtube.com/t/terms/?utm_source=x")
    assert same.id == original.id
    assert lookalike.id != original.id and lookalike.name == "Youtube (youtube.co)"
    assert agent.find_service(conn, "https://www.youtube.com/t/terms").terms_url == "https://www.youtube.com/t/terms"


def test_user_generated_page_on_shared_host_is_its_own_service(conn):
    real = agent.add_service(conn, "Github", "https://docs.github.com/en/site-policy/github-terms/github-terms-of-service")
    fake = agent.add_service(conn, "Github", "https://github.com/attacker/terms")
    assert fake.id != real.id and fake.name == "Github (github.com)"


def test_shared_service_stores_a_canonical_https_url(conn):
    svc = agent.add_service(conn, "Acme", "http://user:pw@WWW.Acme.test/Terms/?ref=evil#top")
    assert svc.terms_url == "https://www.acme.test/Terms"
    again = agent.add_service(conn, "Acme", "https://acme.test/Terms")
    assert again.id == svc.id and again.terms_url == "https://www.acme.test/Terms"


def test_flag_lines_say_what_the_number_means():
    assert agent.flag_line("auto_renews", 0.83) == "🔴 Found (83% sure): auto renews"
    assert agent.flag_line("data_sharing", 0.45) == "🟡 Unclear (45% likely): data sharing"
    assert agent.flag_line("hard_to_cancel", 0.08) == "🟢 Not found (92% sure): hard to cancel"


def test_change_badge_uses_words_and_number():
    v = Verdict(material=0.9, category="data", opt_out=0.1, favours="company", severity=2.6)
    r = agent.ChangeReport(change_id=1, service="S", heading="H", kind="modified",
                           effective_at=datetime(2026, 3, 4, tzinfo=UTC), verdict=v, summary="x", redline="x")
    assert "data · Major (2.6/3) · favours the company" in r.to_html()
    assert "unjudged" in r.model_copy(update={"verdict": None}).to_html()


def test_pack_keeps_cards_whole():
    out = agent.pack(["a" * 1500] * 5, limit=4000)
    assert len(out) == 3 and all(len(m) <= 4000 for m in out) and "".join(out).count("a") == 7500


def test_pack_gives_an_oversized_card_its_own_message():
    assert agent.pack(["x", "y" * 5000, "z"], limit=4000) == ["x", "y" * 5000, "z"]


def report(i, verdict=True, heading=None):
    v = Verdict(material=0.9, category="arbitration", opt_out=0.1, favours="company", severity=2.6) if verdict else None
    return agent.ChangeReport(change_id=i, service="Svc", heading=heading or f"Section {i}", kind="modified",
                              effective_at=datetime(2026, 3, i, tzinfo=UTC), verdict=v, summary=f"Summary {i}",
                              redline="x")


def chat_reply(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})


@respx.mock
async def test_watch_messages_overview_first(conn):
    respx.post(CHAT).mock(return_value=chat_reply("Arbitration <now> & more"))
    svc = agent.add_service(conn, "Svc", "https://svc.test/terms")
    cl = agent.Clients()
    msgs = await agent.watch_messages(cl, svc, [report(1), report(2)])
    await cl.aclose()
    assert len(msgs) == 1
    m = msgs[0].text
    assert msgs[0].rate.kind == "overview" and msgs[0].rate.change_id == 2  # the latest change
    assert m.startswith("<b>Svc</b> — past year (2 changes)")
    assert m.index("Arbitration &lt;now&gt; &amp; more") < m.index("Section 1") < m.index("Section 2")


@respx.mock
async def test_watch_messages_without_overview_when_writer_fails(conn):
    respx.post(CHAT).mock(return_value=chat_reply(None))
    svc = agent.add_service(conn, "Svc", "https://svc.test/terms")
    cl = agent.Clients()
    msgs = await agent.watch_messages(cl, svc, [report(1), report(2)])
    await cl.aclose()
    joined = "\n".join(m.text for m in msgs)
    assert all(m.rate is None for m in msgs)  # no overview written: nothing to rate
    assert "past year (2 changes)" in joined and "Section 1" in joined and "Section 2" in joined
    assert "wrong" not in joined.lower() and "no text" not in joined


@respx.mock
async def test_overview_tolerates_unjudged_reports(conn):
    route = respx.post(CHAT).mock(return_value=chat_reply("ok"))
    svc = agent.add_service(conn, "Svc", "https://svc.test/terms")
    cl = agent.Clients()
    assert await agent.overview(cl, svc, [report(1, verdict=False)]) == "ok"
    await cl.aclose()
    assert "unjudged" in json.loads(route.calls[0].request.content)["messages"][1]["content"]


def test_ai_gateway_allows_long_thinking():
    cl = agent.Clients()
    assert cl.ai.client.timeout.read >= 180


async def test_slow_overview_is_dropped(conn, monkeypatch):
    async def slow(gw, service, facts):
        await asyncio.sleep(1)
        return "too late"

    monkeypatch.setattr(agent.writer, "watch_overview", slow)
    monkeypatch.setattr(agent, "OVERVIEW_TIMEOUT", 0.05)
    svc = agent.add_service(conn, "Svc", "https://svc.test/terms")
    cl = agent.Clients()
    assert await agent.overview(cl, svc, [report(1)]) == ""
    await cl.aclose()


@respx.mock
async def test_overview_is_written_once_per_set_of_changes(conn):
    route = respx.post(CHAT).mock(return_value=chat_reply("Shared overview"))
    svc = agent.add_service(conn, "Svc", "https://svc.test/terms")
    cl = agent.Clients()
    first = await agent.overview(cl, svc, [report(1), report(2)])
    second = await agent.overview(cl, svc, [report(1), report(2)])
    third = await agent.overview(cl, svc, [report(1)])
    await cl.aclose()
    assert first == second == third == "Shared overview"
    assert route.call_count == 2  # the 2-change set once, the 1-change set once


@respx.mock
async def test_failed_overview_is_not_cached(conn):
    route = respx.post(CHAT).mock(side_effect=[chat_reply(None), chat_reply(None), chat_reply("Now it works")])
    svc = agent.add_service(conn, "Svc", "https://svc.test/terms")
    cl = agent.Clients()
    assert await agent.overview(cl, svc, [report(1)]) == ""
    assert await agent.overview(cl, svc, [report(1)]) == "Now it works"
    await cl.aclose()
    assert route.call_count == 3


@respx.mock
async def test_check_url_reports_its_stages(monkeypatch):
    flags = {"model": "jev-1.14", "answers": {k: {"type": "noul", "noul": 0.1} for k in
             ("auto_renews", "arbitration", "data_sharing", "unilateral_changes", "content_licence", "hard_to_cancel")}}
    respx.post(JEV).mock(return_value=httpx.Response(200, json=flags))
    respx.post(CHAT).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": "SIGN"}}]}))

    async def page(cl_, url):
        return "Terms text"

    async def no_grade(q, client=None):
        return None

    monkeypatch.setattr(agent, "page_text", page)
    monkeypatch.setattr(agent.tosdr, "lookup", no_grade)
    stages: list[str] = []
    cl = agent.Clients()
    await agent.check_url(cl, "https://acme.test/terms", on_stage=stages.append)
    await cl.aclose()
    assert stages == ["Fetching the page…", "Checking for red flags…", "Writing it up…"]


# --- Unwatch and undo -----------------------------------------------------------------------------

def _watch(conn, user, svc):
    conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (user.id, svc.id, db.now()))


def test_unwatch_removes_only_that_members_row(conn, members):
    a, b, _ = members
    svc = agent.add_service(conn, "Notion", "https://notion.test/terms")
    _watch(conn, a, svc)
    _watch(conn, b, svc)
    assert agent.unsubscribe(conn, a, svc.id).name == "Notion"
    assert [s.name for s in agent.services_for(conn, a.id)] == []
    assert [s.name for s in agent.services_for(conn, b.id)] == ["Notion"]
    assert agent.unsubscribe(conn, a, svc.id) is None  # already stopped


def test_unwatch_of_another_members_service_changes_nothing(conn, members):
    a, b, _ = members
    svc = agent.add_service(conn, "Only B", "https://b.test/terms")
    _watch(conn, b, svc)
    assert agent.unsubscribe(conn, a, svc.id) is None
    assert agent.unsubscribe(conn, a, 999_999) is None
    assert [s.name for s in agent.services_for(conn, b.id)] == ["Only B"]


def test_last_watcher_leaving_stops_checks_but_keeps_history(conn, members):
    a = members[0]
    svc = agent.add_service(conn, "Lonely", "https://lonely.test/terms")
    _watch(conn, a, svc)
    conn.execute("INSERT INTO snapshots (service_id, taken_at, sha, text, origin) VALUES (?, ?, ?, ?, ?)",
                 (svc.id, db.now(), "abc", "terms", "live"))
    agent.unsubscribe(conn, a, svc.id)
    assert svc.id not in [s.id for s in agent.watched_services(conn)]
    assert conn.execute("SELECT COUNT(*) FROM snapshots WHERE service_id = ?", (svc.id,)).fetchone()[0] == 1


@respx.mock
def test_resubscribe_restores_an_unwatch_once_with_no_provider_calls(conn, members):
    a = members[0]
    svc = agent.add_service(conn, "Notion", "https://notion.test/terms")
    _watch(conn, a, svc)
    agent.unsubscribe(conn, a, svc.id)
    assert agent.resubscribe(conn, a, svc.id).id == svc.id
    assert [s.name for s in agent.services_for(conn, a.id)] == ["Notion"]
    assert agent.resubscribe(conn, a, svc.id) is None  # already restored
    assert agent.resubscribe(conn, a, 999_999) is None
    assert not respx.calls  # nothing fetched, judged or replayed


def test_forged_undo_cannot_add_or_reveal_another_members_service(conn, members):
    a, b, _ = members
    svc = agent.add_service(conn, "Only B", "https://b.test/terms")
    _watch(conn, b, svc)
    assert agent.resubscribe(conn, a, svc.id) is None  # A never watched it
    assert agent.services_for(conn, a.id) == []
    agent.unsubscribe(conn, b, svc.id)
    assert agent.resubscribe(conn, a, svc.id) is None  # B's unwatch is not A's to undo


# --- Verdict cards: quotes and explain buttons -----------------------------------------------------

DOC = "Section 9. Binding arbitration applies to any dispute arising from these terms. You may opt out by mail."


def _flags(**found):
    keys = ("auto_renews", "arbitration", "data_sharing", "unilateral_changes", "content_licence", "hard_to_cancel")
    return {"model": "jev-1.14", "answers": {k: {"type": "noul", "noul": found.get(k, 0.05)} for k in keys}}


@respx.mock
async def test_verified_quote_is_shown_and_invented_one_is_not():
    respx.post(JEV).mock(return_value=httpx.Response(200, json=_flags(arbitration=0.92, data_sharing=0.8, auto_renews=0.4)))
    reply = ("PUSH BACK\n- **Arbitration:** no court\n"
             "QUOTE arbitration: Binding arbitration applies to any dispute arising from these terms.\n"
             "QUOTE data_sharing: We share your data with every advertiser we can find.")
    chat = respx.post(CHAT).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": reply}}]}))
    cl = agent.Clients()
    card = await agent.verdict_card(cl, "Lease", DOC)
    await cl.aclose()
    assert "QUOTE" not in card.text
    assert "<blockquote expandable><b>What the document says</b>\n<b>arbitration</b>: “Binding arbitration" in card.text
    assert "every advertiser" not in card.text
    assert (card.rate.kind, card.rate.prompt_version, card.rate.quotes_ok) == ("verdict", "v2", False)  # one dropped
    assert card.buttons == [[("💡 auto renews", "v1:fx:auto_renews"), ("💡 arbitration", "v1:fx:arbitration")],
                            [("💡 data sharing", "v1:fx:data_sharing")]]
    prompt = json.loads(chat.calls[0].request.content)["messages"][1]["content"]
    assert "QUOTE <key>:" in prompt and "arbitration, data_sharing" in prompt


@respx.mock
async def test_no_found_flags_means_no_quotes_asked():
    respx.post(JEV).mock(return_value=httpx.Response(200, json=_flags()))
    chat = respx.post(CHAT).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": "SIGN"}}]}))
    cl = agent.Clients()
    card = await agent.verdict_card(cl, "Lease", DOC)
    await cl.aclose()
    assert "QUOTE" not in json.loads(chat.calls[0].request.content)["messages"][1]["content"]
    assert "What the document says" not in card.text and card.buttons == []
    assert card.rate.quotes_ok is None  # no flag found, so no quotes were asked for


@respx.mock
async def test_long_card_never_splits_a_blockquote():
    from app.telegram import split

    respx.post(JEV).mock(return_value=httpx.Response(200, json=_flags(arbitration=0.92)))
    long_card = "PUSH BACK\n" + "\n\n".join("- " + "word " * 150 for _ in range(6))
    reply = long_card + "\nQUOTE arbitration: Binding arbitration applies to any dispute arising from these terms."
    respx.post(CHAT).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": reply}}]}))
    cl = agent.Clients()
    card = await agent.verdict_card(cl, "Lease", DOC)
    await cl.aclose()
    chunks = split(card.text)
    assert len(chunks) > 1
    assert all(c.count("<blockquote") == c.count("</blockquote>") for c in chunks)


# --- Review fixes ------------------------------------------------------------------------------------

def test_alerts_are_rated_only_when_the_writer_wrote_them(conn):
    svc = agent.add_service(conn, "Svc", "https://svc.test/terms")
    written = agent.alert(report(1), svc)
    raw = agent.alert(report(2).model_copy(update={"written": False}), svc)
    assert written.rate.kind == "alert" and raw.rate is None
    assert raw.buttons == written.buttons == [[agent.unwatch_button(svc)]]


@respx.mock
async def test_tick_alert_is_not_rated_when_the_writer_fails(conn, members, history, monkeypatch):
    m = mock_providers()
    a = members[0]
    cl = agent.Clients()
    await agent.subscribe(conn, cl, a, "Acme", "https://acme.test/terms")
    # The judge still works, but the writer returns nothing: the summary falls back to the redline.
    m["chat"].mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": None}, "finish_reason": "length"}]}))
    changed = text(NEW_HTML).replace("individual arbitration", "individual arbitration in Texas, with no right to appeal")

    async def page(cl_, url):
        return changed

    monkeypatch.setattr(agent, "page_text", page)
    result = await agent.tick(conn, cl)
    await cl.aclose()
    alerts = [o for o in result.deliveries.get(a.id, []) if o.buttons]
    assert alerts and all(o.rate is None for o in alerts)


def test_shown_quote_drops_its_own_curly_quotes():
    from app.quotes import shown

    assert shown("“Binding arbitration applies.”") == "Binding arbitration applies"

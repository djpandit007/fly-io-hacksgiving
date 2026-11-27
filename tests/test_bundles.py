"""Policy Bundles: several documents per service, judged once, labelled by role, checked for contradictions,
and searchable by /ask."""

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from app import agent, ask, ask_tools, db, users
from app.ask_tools import AskDeps, Clause
from tests.test_judge import GOOD

JEV = "https://opencode.test/inference/systemone/v1/systemone"
CHAT = "https://opencode.test/inference/openai/v1/chat/completions"
SUMMARY = "Acme may now train its AI on your content."

TERMS_URL = "https://acme.test/terms"
AI_URL = "https://acme.test/ai-terms"
TERMS = (
    "## 1. Your Content\nYou own your content. We never use your content to train our artificial intelligence models.\n"
    "## 2. Payments\nFees are billed monthly in advance."
)
AI_OLD = "## Model Training\nThis page describes how our assistant features work for workspace owners."
AI_NEW = (
    "## Model Training\nYou grant us a perpetual worldwide licence to use your content to train our artificial "
    "intelligence models."
)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "b.db")


@pytest.fixture
def members(conn):
    return [users.join_member(conn, "join-code", chat_id=i, name=n) for i, n in ((1, "A"), (2, "B"), (3, "C"))]


@pytest.fixture
def pages(monkeypatch):
    """Acme's terms link to its AI Terms; no archive history; live pages come from `pages`."""
    live = {TERMS_URL: TERMS, AI_URL: AI_OLD}
    backfills: list[tuple[str, str | None]] = []

    async def fake_page_text(cl, url):
        if isinstance(live[url], Exception):
            raise live[url]
        return live[url]

    async def fake_backfill(name, url, days=365, document="Terms of Service"):
        backfills.append((url, document))
        return []

    async def companions(terms_url, client=None):
        return {"ai_terms": AI_URL}

    async def no_grade(q, client=None):
        return None

    monkeypatch.setattr(agent, "page_text", fake_page_text)
    monkeypatch.setattr(agent, "backfill", fake_backfill)
    monkeypatch.setattr(agent.discover, "discover_companions", companions)
    monkeypatch.setattr(agent.tosdr, "lookup", no_grade)
    live["backfills"] = backfills
    return live


def providers(contradicts: float = 0.9, severity: float = 2.6):
    """Jev answers change judgments with GOOD and contradiction batches with `contradicts`/`severity`."""
    calls = {"judge": 0, "contradiction": 0}

    def jev(request):
        questions = json.loads(request.content)["questions"]
        if any(k.startswith("contradicts_") for k in questions):
            calls["contradiction"] += 1
            answers = {
                k: {"type": "noul", "noul": contradicts} if k.startswith("contradicts_")
                else {"type": "score", "score": severity, "confidence": 0.7, "probabilities": {}}
                for k in questions
            }
            return httpx.Response(200, json={"model": "jev-1.14", "answers": answers})
        calls["judge"] += 1
        return httpx.Response(200, json=GOOD)

    respx.post(JEV).mock(side_effect=jev)
    respx.post(CHAT).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": SUMMARY}}]}))
    return calls


def clients():
    cl = agent.Clients()
    cl.pinecone = None  # precedents are covered elsewhere
    return cl


async def watch_acme(conn, cl, *watchers):
    for user in watchers:
        svc, _ = await agent.subscribe(conn, cl, user, "Acme", TERMS_URL)
    return svc


# --- Watching and checking a bundle ----------------------------------------------------------------

@respx.mock
async def test_watch_attaches_discovered_companions_and_says_which_are_watched(conn, members, pages):
    providers()
    cl = clients()
    svc = await watch_acme(conn, cl, members[0])
    await cl.aclose()
    docs = agent.documents_for(conn, svc)
    assert [(d.role, d.url) for d in docs] == [("terms", TERMS_URL), ("ai_terms", AI_URL)]
    assert agent.documents_line(conn, svc) == "📄 Documents watched: Terms, AI Terms"
    stored = conn.execute("SELECT document_id, text FROM snapshots ORDER BY id").fetchall()
    assert [(r["document_id"], r["text"]) for r in stored] == [(docs[0].id, TERMS), (docs[1].id, AI_OLD)]
    # The terms backfill as before; AI Terms have no archive document type, so Wayback only.
    assert pages["backfills"] == [(TERMS_URL, "Terms of Service"), (AI_URL, None)]


@respx.mock
async def test_companion_change_is_judged_once_and_labelled_for_every_watcher(conn, members, pages):
    calls = providers(contradicts=0.1)
    a, b, c = members
    cl = clients()
    svc = await watch_acme(conn, cl, a, b)
    pages[AI_URL] = AI_NEW
    result = await agent.tick(conn, cl)
    await cl.aclose()

    assert result.checked == 1 and result.changes == 1 and result.contradictions == 0
    assert calls["judge"] == 1  # one changed section, judged once for two watchers
    assert set(result.deliveries) == {a.id, b.id} and c.id not in result.deliveries
    for msgs in result.deliveries.values():
        assert len(msgs) == 1
        assert msgs[0].text.startswith("<b>Acme (AI Terms)</b> changed <i>Model Training</i>")
    ai_doc = agent.documents_for(conn, svc)[1]
    assert conn.execute("SELECT document_id FROM changes").fetchone()[0] == ai_doc.id
    # The judge is told which document changed.
    judged = [json.loads(r.request.content) for r in respx.calls if "material" in json.loads(r.request.content).get("questions", {})]
    assert judged[0]["state"]["service"] == "Acme (AI Terms)"


@respx.mock
async def test_unreadable_companion_does_not_fail_the_check(conn, members, pages):
    providers()
    cl = clients()
    await watch_acme(conn, cl, members[0])
    pages[AI_URL] = httpx.ConnectError("down")
    result = await agent.tick(conn, cl)
    await cl.aclose()
    assert result.checked == 1 and result.failed == []


@respx.mock
async def test_service_with_only_terms_and_pre_bundle_snapshots_keeps_working(conn, members, monkeypatch):
    providers()
    old = "## Disputes\nDisputes are resolved in the courts of California."
    new = "## Disputes\nDisputes are resolved by binding individual arbitration in Texas."
    svc = agent.add_service(conn, "Acme", TERMS_URL)
    # A snapshot stored before Policy Bundles: no document.
    conn.execute("INSERT INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (?, ?, 'live', 'old', ?)",
                 (svc.id, (datetime.now(UTC) - timedelta(days=3)).isoformat(), old))

    async def page(cl_, url):
        assert url == TERMS_URL
        return new

    monkeypatch.setattr(agent, "page_text", page)
    cl = clients()
    changes = await agent.check_service(conn, cl, svc)
    report = await agent.report_for(conn, cl, changes[0], svc, members[0])
    await cl.aclose()
    assert [c.heading for c in changes] == ["Disputes"]
    assert changes[0].document_id == agent.documents_for(conn, svc)[0].id
    assert report.doc_role == "terms" and report.to_html().startswith("<b>Acme</b> changed <i>Disputes</i>")


# --- Contradictions ----------------------------------------------------------------------------------

@respx.mock
async def test_contradiction_is_stored_once_and_alerted_with_both_documents_quotes(conn, members, pages):
    calls = providers(contradicts=0.92, severity=2.7)
    a, b, _ = members
    cl = clients()
    svc = await watch_acme(conn, cl, a, b)
    pages[AI_URL] = AI_NEW
    result = await agent.tick(conn, cl)

    assert result.contradictions == 1 and calls["contradiction"] == 1
    [row] = db.rows(db.Contradiction, conn.execute("SELECT * FROM document_contradictions"))
    terms_doc, ai_doc = agent.documents_for(conn, svc)
    assert (row.doc_a_id, row.doc_b_id) == (terms_doc.id, ai_doc.id)  # lower id first, whichever side changed
    assert (row.heading_a, row.heading_b) == ("1. Your Content", "Model Training")
    assert row.quote_a == "We never use your content to train our artificial intelligence models."
    assert row.quote_b.startswith("You grant us a perpetual worldwide licence") and row.severity == 2.7
    for user in (a, b):
        alert = result.deliveries[user.id][-1]
        assert alert.text.startswith("⚠️ <b>Acme</b>: its Terms and AI Terms contradict each other")
        assert "<b>Terms · 1. Your Content</b>\n“We never use your content" in alert.text
        assert "<b>AI Terms · Model Training</b>\n“You grant us a perpetual" in alert.text
        assert "Major (2.7/3)" in alert.text and alert.buttons == [[agent.unwatch_button(svc)]]

    # Found again (the next check, or from the other side): stored and alerted only once.
    again = await agent.tick(conn, cl)
    stored_change = db.rows(db.Change, conn.execute("SELECT * FROM changes"))
    assert await agent.find_contradictions(conn, cl, svc, stored_change) == []
    await cl.aclose()
    assert again.contradictions == 0 and again.deliveries == {}
    assert conn.execute("SELECT COUNT(*) FROM document_contradictions").fetchone()[0] == 1


@respx.mock
async def test_documents_that_agree_stay_quiet(conn, members, pages):
    calls = providers(contradicts=0.3)
    cl = clients()
    await watch_acme(conn, cl, members[0])
    pages[AI_URL] = AI_NEW
    result = await agent.tick(conn, cl)
    await cl.aclose()
    assert calls["contradiction"] == 1  # asked, and Jev was not sure enough
    assert result.contradictions == 0
    assert conn.execute("SELECT COUNT(*) FROM document_contradictions").fetchone()[0] == 0
    assert not any("contradict" in m.text for m in result.deliveries[members[0].id])


@respx.mock
async def test_unrelated_sections_are_not_sent_to_the_judge(conn, members, pages):
    calls = providers()
    cl = clients()
    await watch_acme(conn, cl, members[0])
    pages[AI_URL] = "## Model Training\nOur office hours are nine to five on weekdays, except public holidays."
    await agent.tick(conn, cl)
    await cl.aclose()
    assert calls["judge"] == 1 and calls["contradiction"] == 0  # no candidate shares enough words


def test_candidates_are_bounded_and_on_topic(conn):
    svc = agent.add_service(conn, "Acme", TERMS_URL)
    agent.add_documents(conn, svc, {"privacy": "https://acme.test/privacy"})
    privacy = agent.documents_for(conn, svc)[1]
    other = "\n".join(f"## S{i}\nWe use your content and data to train artificial intelligence models, part {i}."
                      for i in range(10))
    change = db.Change(id=1, service_id=svc.id, from_snap=None, to_snap=1, detected_at=datetime.now(UTC),
                       effective_at=datetime.now(UTC), heading="Training", kind="added", before_text="",
                       after_text=AI_NEW, category="data")
    picked = agent.contradiction_candidates(change, [(privacy, other + "\n## Office\nOpen nine to five.")])
    assert len(picked) == 3 and all(h.startswith("S") for _, h, _ in picked)


# --- /ask across the bundle --------------------------------------------------------------------------

PRIVACY = "## Data Usage\nWe use workspace content to improve and train our AI features unless you opt out."
DATA_QUOTE = "We use workspace content to improve and train our AI features"


@pytest.fixture
def notion(conn, members):
    svc = agent.add_service(conn, "Notion", "https://notion.test/terms")
    agent.add_documents(conn, svc, {"ai_terms": "https://notion.test/ai"})
    _, ai_doc = agent.documents_for(conn, svc)
    when = datetime(2026, 11, 27, 9, tzinfo=UTC).isoformat()
    conn.execute("INSERT INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (?, ?, 'live', 't', ?)",
                 (svc.id, when, "## Payments\nNotion bills the workspace owner every month."))
    conn.execute(
        "INSERT INTO snapshots (service_id, taken_at, origin, sha, text, document_id) VALUES (?, ?, 'live', 'a', ?, ?)",
        (svc.id, when, PRIVACY, ai_doc.id),
    )
    conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)",
                 (members[0].id, svc.id, db.now()))
    return AskDeps(conn, members[0].id)


def test_ask_lookups_span_every_document(notion):
    assert ask_tools.list_documents(notion, "Notion") == ["terms", "ai_terms"]
    assert ask_tools.list_sections(notion, "Notion") == ["Payments"]
    assert ask_tools.list_sections(notion, "Notion", "AI Terms") == ["Data Usage"]
    assert ask_tools.list_sections(notion, "Notion", "contract") == []
    assert DATA_QUOTE in ask_tools.read_section(notion, "Notion", "Data Usage", document="ai_terms")
    [hit] = ask_tools.search_terms(notion, "train AI")
    assert (hit.document, hit.heading) == ("ai_terms", "Data Usage")


def test_quotes_are_verified_against_the_document_they_cite(notion):
    right = Clause(service="Notion", heading="Data Usage", quote=DATA_QUOTE, document="AI Terms")
    wrong = right.model_copy(update={"document": "terms"})
    [kept] = ask_tools.verified(notion, [right, wrong])
    assert kept.document == "ai_terms"
    on = ask_tools.dated(notion, kept)
    assert on == date(2026, 11, 27)
    assert ask._label(kept, on) == "Notion (AI Terms) · Data Usage · as of 27 Nov 2026"


def test_change_clause_takes_its_document_from_the_database(conn, notion):
    svc = agent.find_service(conn, "https://notion.test/terms")
    ai_doc = agent.documents_for(conn, svc)[1]
    change_id = conn.execute(
        """INSERT INTO changes (service_id, from_snap, to_snap, detected_at, effective_at, heading, kind,
                                before_text, after_text, material, summary, document_id)
           VALUES (?, NULL, 1, ?, ?, 'Data Usage', 'modified', '', ?, 0.9, 's', ?)""",
        (svc.id, db.now(), db.now(), PRIVACY.split("\n", 1)[1], ai_doc.id),
    ).lastrowid
    claimed = Clause(service="Notion", heading="Made up", quote=DATA_QUOTE, source="change", change_id=change_id)
    [kept] = ask_tools.verified(notion, [claimed])
    assert (kept.document, kept.heading) == ("ai_terms", "Data Usage")
    assert ask_tools.recent_changes(notion, "Notion")[0].document == "ai_terms"

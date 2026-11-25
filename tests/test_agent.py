"""End to end through record_version with OpenCode (Jev + chat) and Pinecone mocked at the HTTP layer."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app import agent, db, ledger
from app.watch import html_to_markdown, normalise
from tests.test_judge import GOOD
from tests.test_watch import NEW_HTML, OLD_HTML

JEV = "https://opencode.test/inference/systemone/v1/systemone"
CHAT = "https://opencode.test/inference/openai/v1/chat/completions"


def text(html):
    return normalise(html_to_markdown(html))


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "a.db")


@respx.mock
async def test_change_is_judged_summarised_remembered_and_deadlined(conn):
    jev = respx.post(JEV).mock(return_value=httpx.Response(200, json=GOOD))
    chat = respx.post(CHAT).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Disputes now go to arbitration; opt out within 30 days."}}]})
    )
    respx.post("https://pinecone.test/embed").mock(
        return_value=httpx.Response(200, json={"model": "llama-text-embed-v2", "data": [{"values": [0.1] * 1024}]})
    )
    search = respx.post("https://pinecone.test/indexes/fine-print/query").mock(
        return_value=httpx.Response(200, json={"matches": [
            {"id": "chg-9", "score": 0.83, "metadata": {"service": "Spotify", "heading": "Disputes", "text": "..."}},
            {"id": "chg-8", "score": 0.2, "metadata": {"service": "Weak"}},
        ]})
    )
    upsert = respx.post("https://pinecone.test/indexes/fine-print/vectors/upsert").mock(
        return_value=httpx.Response(200, json={"upsertedCount": 1})
    )

    cl = agent.Clients()
    svc = agent.add_service(conn, "Acme", "https://acme.test/terms")
    effective = datetime.now(UTC) - timedelta(days=1)

    assert await agent.record_version(conn, cl, svc, text(OLD_HTML), effective - timedelta(days=200), "wayback") == []
    reports = await agent.record_version(conn, cl, svc, text(NEW_HTML), effective, "live")
    await cl.aclose()

    assert {r.heading for r in reports} == {"2. Payments", "3. Arbitration"}
    arb = next(r for r in reports if r.heading == "3. Arbitration")
    assert arb.summary.startswith("Disputes now go to arbitration")
    assert [p.service for p in arb.precedents] == ["Spotify"]  # low-score hit filtered out
    assert arb.deadline == (effective + timedelta(days=30)).date()
    assert "Opt out: Acme" in ledger.upcoming(conn)[0].title
    assert "Spotify" in arb.to_html()

    # Pinecone: vectors go to the named index and namespace, with the clause in metadata.
    sent = json.loads(upsert.calls[0].request.content)
    assert sent["namespace"] == "clauses" and len(sent["vectors"][0]["values"]) == 1024
    assert sent["vectors"][0]["metadata"]["service"] == "Acme"
    # Direct mode sends provider-specific auth.
    assert jev.calls[0].request.headers["authorization"] == "Bearer oc-key"
    assert json.loads(jev.calls[0].request.content)["model"] == "jev-1.14"
    assert json.loads(chat.calls[0].request.content)["model"] == "glm-5.3-flash"
    assert search.calls[0].request.headers["api-key"] == "pc-key"
    assert chat.called


@respx.mock
async def test_unchanged_text_does_no_work(conn):
    jev = respx.post(JEV).mock(return_value=httpx.Response(200, json=GOOD))
    cl = agent.Clients()
    svc = agent.add_service(conn, "Acme", "https://acme.test/terms")
    now = datetime.now(UTC)
    await agent.record_version(conn, cl, svc, text(OLD_HTML), now - timedelta(days=1), "live")
    assert await agent.record_version(conn, cl, svc, text(OLD_HTML), now, "live") == []
    await cl.aclose()
    assert not jev.called


@respx.mock
async def test_immaterial_change_is_stored_but_not_reported(conn):
    quiet = {**GOOD, "answers": {**GOOD["answers"], "material": {"type": "noul", "noul": 0.05}}}
    respx.post(JEV).mock(return_value=httpx.Response(200, json=quiet))
    chat = respx.post(CHAT)
    cl = agent.Clients()
    svc = agent.add_service(conn, "Acme", "https://acme.test/terms")
    now = datetime.now(UTC)
    await agent.record_version(conn, cl, svc, text(OLD_HTML), now - timedelta(days=1), "live")
    assert await agent.record_version(conn, cl, svc, text(NEW_HTML), now, "live") == []
    await cl.aclose()
    assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 2
    assert not chat.called


@respx.mock
async def test_verdict_card_survives_writer_failure():
    flags = {"model": "jev-1.14", "answers": {k: {"type": "noul", "noul": 0.9} for k in
             ("auto_renews", "arbitration", "data_sharing", "unilateral_changes", "content_licence", "hard_to_cancel")}}
    respx.post(JEV).mock(return_value=httpx.Response(200, json=flags))
    respx.post(CHAT).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": None}, "finish_reason": "length"}]}))
    cl = agent.Clients()
    card = await agent.verdict_card(cl, "Lease", "Some lease text")
    await cl.aclose()
    assert "🔴 arbitration (90%)" in card and "not legal advice" in card

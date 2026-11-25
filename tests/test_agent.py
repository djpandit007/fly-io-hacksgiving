"""End to end through record_version with TypeSafe, Ollama and Pinecone mocked at the HTTP layer."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app import agent, db, ledger
from app.watch import html_to_markdown, normalise
from tests.test_judge import GOOD
from tests.test_watch import NEW_HTML, OLD_HTML


def text(html):
    return normalise(html_to_markdown(html))


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "a.db")


@respx.mock
async def test_change_is_judged_summarised_remembered_and_deadlined(conn):
    jev = respx.post("https://typesafe.test/v1/systemone").mock(return_value=httpx.Response(200, json=GOOD))
    ollama = respx.post("https://ollama.test/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"role": "assistant", "content": "Disputes now go to arbitration; opt out within 30 days."}})
    )
    search = respx.post("https://pinecone.test/records/namespaces/clauses/search").mock(
        return_value=httpx.Response(200, json={"result": {"hits": [
            {"_id": "chg-9", "_score": 0.83, "fields": {"service": "Spotify", "heading": "Disputes", "chunk_text": "..."}},
            {"_id": "chg-8", "_score": 0.2, "fields": {"service": "Weak", "heading": "", "chunk_text": ""}},
        ]}})
    )
    upsert = respx.post("https://pinecone.test/records/namespaces/clauses/upsert").mock(return_value=httpx.Response(201))

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

    # Pinecone upsert is NDJSON with integrated-embedding field name.
    assert upsert.calls[0].request.headers["content-type"] == "application/x-ndjson"
    assert b'"chunk_text"' in upsert.calls[0].request.content
    # Direct mode sends provider-specific auth.
    assert jev.calls[0].request.headers["authorization"] == "Bearer ts-key"
    assert search.calls[0].request.headers["api-key"] == "pc-key"
    assert ollama.called


@respx.mock
async def test_unchanged_text_does_no_work(conn):
    jev = respx.post("https://typesafe.test/v1/systemone").mock(return_value=httpx.Response(200, json=GOOD))
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
    respx.post("https://typesafe.test/v1/systemone").mock(return_value=httpx.Response(200, json=quiet))
    ollama = respx.post("https://ollama.test/api/chat")
    cl = agent.Clients()
    svc = agent.add_service(conn, "Acme", "https://acme.test/terms")
    now = datetime.now(UTC)
    await agent.record_version(conn, cl, svc, text(OLD_HTML), now - timedelta(days=1), "live")
    assert await agent.record_version(conn, cl, svc, text(NEW_HTML), now, "live") == []
    await cl.aclose()
    assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 2
    assert not ollama.called

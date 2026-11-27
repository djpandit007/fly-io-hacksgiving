"""Clause market benchmarks: deterministic percentiles, small-cohort omission, read-only queries."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app import agent, benchmark, db, memory
from app.benchmark import (
    MIN_COHORT,
    Benchmark,
    benchmark_clause,
    from_matches,
    percentile,
)
from app.config import Provider
from app.gateway import Gateway
from tests.test_judge import GOOD
from tests.test_watch import NEW_HTML, OLD_HTML

EMBED = "https://pinecone.test/embed"
QUERY = "https://pinecone.test/indexes/fine-print/query"
UPSERT = "https://pinecone.test/indexes/fine-print/vectors/upsert"
JEV = "https://opencode.test/inference/systemone/v1/systemone"
CHAT = "https://opencode.test/inference/openai/v1/chat/completions"


def match(service, severity, score=0.8, public=True, opt_out=False, category="arbitration"):
    return {"id": f"chg-{service}-{severity}", "score": score, "metadata": {
        "service": service, "category": category, "severity": severity, "scale": "clause", "public": public,
        "has_opt_out": opt_out}}


def matches(n, **kw):
    return [memory.Match.model_validate(match(f"S{i}", i * 0.25, score=0.9 - i / 100, **kw)) for i in range(n)]


def test_percentile_is_the_share_strictly_less_severe():
    assert percentile(2.0, [0.0, 1.0, 2.0, 3.0]) == 50.0
    assert percentile(3.5, [1.0, 2.0]) == 100.0
    assert percentile(0.0, [0.0, 1.0]) == 0.0
    assert percentile(1.0, []) == 0.0


def test_benchmark_over_a_full_cohort():
    found = from_matches(matches(12, opt_out=False), severity=2.0)
    # severities 0, 0.25, ..., 2.75: eight are below 2.0
    assert found == Benchmark(percentile=100 * 8 / 12, cohort_size=12, opt_out_share=0.0,
                              comparable_services=["S0", "S1", "S2"])
    assert "More severe than 67% of comparable arbitration clauses across 12 public services." == found.line("arbitration")


def test_small_cohort_is_omitted_not_guessed():
    assert from_matches(matches(MIN_COHORT - 1), severity=2.0) is None
    # Many clauses from few services are still a small cohort: one clause counts per service.
    same = [memory.Match.model_validate(match("Spotify", s / 10)) for s in range(30)]
    assert from_matches(same, severity=2.0) is None


def test_cohort_skips_private_and_excluded_services_and_keeps_the_nearest_per_service():
    ms = matches(10)
    ms.append(memory.Match.model_validate(match("Private", 0.0, public=False)))
    assert from_matches(ms, 2.0, exclude_service="S0") is None  # 9 public services left
    extra = memory.Match.model_validate(match("S1", 3.0, score=0.1))  # S1's farther clause is ignored
    found = from_matches([*matches(10), extra], severity=0.3)
    assert found.cohort_size == 10 and found.percentile == 20.0  # S0 (0.0) and S1 (0.25) are below 0.3


def test_opt_out_share():
    ms = [memory.Match.model_validate(match(f"S{i}", 1.0, opt_out=i < 3)) for i in range(10)]
    assert from_matches(ms, 2.0).opt_out_share == pytest.approx(0.3)


def jev_with_clause_severity(score):
    """GOOD for change verdicts; `score` for clause-severity questions (s1, s2, ...)."""
    def answer(request):
        questions = json.loads(request.content)["questions"]
        if "s1" in questions:
            return httpx.Response(200, json={"model": "jev", "answers": {
                k: {"type": "score", "score": score, "confidence": 0.7, "probabilities": {}} for k in questions}})
        return httpx.Response(200, json=GOOD)

    return answer


def _gw():
    return Gateway(Provider(name="pinecone", base_url="https://pinecone.test", key="pc-key"))


@respx.mock
async def test_benchmark_clause_only_queries_public_clauses_of_the_category():
    respx.post(EMBED).mock(return_value=httpx.Response(200, json={"data": [{"values": [0.1] * 1024}]}))
    query = respx.post(QUERY).mock(return_value=httpx.Response(200, json={
        "matches": [match(f"S{i}", i * 0.25) for i in range(12)]}))
    upsert = respx.post(UPSERT).mock(return_value=httpx.Response(200, json={}))
    gw = _gw()
    found = await benchmark_clause(gw, "Disputes go to arbitration.", "arbitration", 2.6, exclude_service="Acme")
    await gw.aclose()
    assert found and found.cohort_size == 12
    body = json.loads(query.calls.last.request.content)
    assert body["topK"] == 50 and body["filter"] == {"public": True, "category": "arbitration", "scale": "clause"}
    assert not upsert.called


@respx.mock
async def test_benchmark_clause_returns_none_for_a_small_cohort():
    respx.post(QUERY).mock(return_value=httpx.Response(200, json={"matches": [match("Only", 1.0)]}))
    gw = _gw()
    assert await benchmark_clause(gw, "x", "fees", 2.0, vector=[0.1] * 1024) is None
    await gw.aclose()


# --- Public clauses are indexed with benchmark metadata; alerts show the line ---------------------

def _text(html):
    from app.watch import html_to_markdown, normalise

    return normalise(html_to_markdown(html))


@respx.mock
async def test_public_material_clause_is_upserted_with_benchmark_metadata(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    respx.post(JEV).mock(side_effect=jev_with_clause_severity(1.4))
    respx.post(CHAT).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": "Summary"}}]}))
    respx.post(EMBED).mock(return_value=httpx.Response(200, json={"data": [{"values": [0.1] * 1024}]}))
    upsert = respx.post(UPSERT).mock(return_value=httpx.Response(200, json={"upsertedCount": 1}))
    svc = agent.add_service(conn, "Acme", "https://acme.test/terms")
    cl = agent.Clients()
    now = datetime.now(UTC)
    await agent.record_version(conn, cl, svc, _text(OLD_HTML), now - timedelta(days=2), "live")
    await agent.record_version(conn, cl, svc, _text(NEW_HTML), now - timedelta(days=1), "live")
    await cl.aclose()
    meta = json.loads(upsert.calls[0].request.content)["vectors"][0]["metadata"]
    assert meta["public"] is True and meta["domain"] == "consumer_tos"
    # The clause's own severity (1.4), not the change's (2.6): the scale private clauses are compared on.
    assert (meta["severity"], meta["scale"], meta["favours"], meta["has_opt_out"]) == (1.4, "clause", "company", True)


def test_vectors_without_the_clause_scale_stay_out_of_the_cohort():
    old = [memory.Match.model_validate({**match(f"S{i}", 1.0), "metadata": {**match(f"S{i}", 1.0)["metadata"], "scale": ""}})
           for i in range(12)]
    assert from_matches(old, 2.0) is None


def test_clause_metadata_without_a_verdict():
    assert benchmark.clause_metadata(None) == {"domain": "consumer_tos"}


def _change(svc, change_id, heading, after, category, severity):
    return db.Change(
        id=change_id, service_id=svc.id, from_snap=None, to_snap=1, detected_at=datetime.now(UTC),
        effective_at=datetime.now(UTC), heading=heading, kind="modified", before_text="old", after_text=after,
        material=0.9, category=category, opt_out=0.1, severity=severity, favours="company", summary="Summary.",
    )


@respx.mock
async def test_alert_shows_a_benchmark_once_per_change_for_every_watcher(tmp_path):
    from app import users

    conn = db.connect(tmp_path / "c.db")
    a = users.join_member(conn, "join-code", chat_id=1, name="A")
    b = users.join_member(conn, "join-code", chat_id=2, name="B")
    svc = agent.add_service(conn, "Acme", "https://acme.test/terms")
    change = _change(svc, 1, "3. Arbitration", "Disputes go to binding arbitration.", "arbitration", 2.6)
    respx.post(EMBED).mock(return_value=httpx.Response(200, json={"data": [{"values": [0.1] * 1024}]}))
    jev = respx.post(JEV).mock(side_effect=jev_with_clause_severity(2.0))
    query = respx.post(QUERY).mock(return_value=httpx.Response(200, json={
        "matches": [match(f"S{i}", i * 0.25) for i in range(12)]}))
    cl = agent.Clients()
    vectors, benches = {}, {}
    ra = await agent.report_for(conn, cl, change, svc, a, vectors, benches)
    rb = await agent.report_for(conn, cl, change, svc, b, vectors, benches)
    await cl.aclose()
    assert "📊 More severe than" in ra.to_html() and ra.benchmark == rb.benchmark
    assert query.call_count == 1 and jev.call_count == 1  # benchmarked once, shared by both watchers
    assert json.loads(query.calls.last.request.content)["filter"]["scale"] == "clause"


@respx.mock
async def test_benchmark_failure_never_blocks_an_alert(tmp_path):
    from app import users

    conn = db.connect(tmp_path / "d.db")
    a = users.join_member(conn, "join-code", chat_id=1, name="A")
    svc = agent.add_service(conn, "Acme", "https://acme.test/terms")
    change = _change(svc, 2, "Fees", "New fees apply.", "fees", 2.0)
    respx.post(EMBED).mock(return_value=httpx.Response(400))
    cl = agent.Clients()
    report = await agent.report_for(conn, cl, change, svc, a, {}, {})
    await cl.aclose()
    assert report.benchmark is None and "📊" not in report.to_html()

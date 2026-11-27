"""Worker Sprites: DNS egress lock, exec, base-checkpoint restore in `finally`, telemetry, Scout fallback."""

import base64
import json

import httpx
import pytest
import respx

from app import agent, db, metrics, swarm
from app.config import Provider, settings
from workers import scout_runner

API = "https://sprites.test"
SCOUT = f"{API}/v1/sprites/fp-scout"
LONG_HTML = "<html><body><main><h2>Terms of Service</h2>" + "<p>You agree to these terms. </p>" * 40 + "</main></body></html>"
PNG = swarm.PNG_MAGIC + b"fake-png-bytes"
URL = "https://acme.test/terms"


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "swarm.db")


def make(conn=None, checkpoints=None) -> swarm.Swarm:
    return swarm.Swarm(
        Provider(name="sprites", base_url=API, key="tok"),
        conn=conn,
        sprites={"scout": "fp-scout", "advocate": "fp-advocate"},
        checkpoints=checkpoints if checkpoints is not None else {"scout": "v1", "advocate": "v2"},
    )


def framed(result: dict) -> bytes:
    """Exec output the way non-TTY streams frame it: a stream byte, then the bytes."""
    line = scout_runner.encode_result(result).encode()
    half = len(line) // 2
    return b"\x01some log line\n\x01" + line[:half] + b"\x01" + line[half:] + b"\n\x03\x00"


def mock_api(exec_response: httpx.Response | Exception) -> dict[str, respx.Route]:
    return {
        "policy": respx.post(f"{SCOUT}/policy/network").mock(return_value=httpx.Response(200, json={"rules": []})),
        "exec": respx.post(f"{SCOUT}/exec").mock(
            side_effect=exec_response if isinstance(exec_response, Exception) else None,
            return_value=None if isinstance(exec_response, Exception) else exec_response,
        ),
        "restore": respx.post(f"{SCOUT}/checkpoints/v1/restore").mock(return_value=httpx.Response(
            200, text='{"type":"info","data":"Restoring","time":"t"}\n{"type":"complete","data":"Restored to v1","time":"t"}\n'
        )),
    }


def ok_result(**kw) -> dict:
    base = {"ok": True, "final_url": URL, "html": LONG_HTML, "text": "terms", "sha256": "0" * 64, "links": []}
    return base | kw


@pytest.fixture
def public_dns(monkeypatch):
    async def public(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(swarm, "resolve_public", public)


# --- Egress policy ---------------------------------------------------------------------------------

def test_egress_hosts_are_the_target_site_plus_capped_extras():
    assert swarm.egress_hosts("https://www.notion.so/terms") == ["www.notion.so", "notion.so", "*.notion.so"]
    assert swarm.egress_hosts("https://a.b.bbc.co.uk/x", ["cdn.example.net"])[-1] == "cdn.example.net"
    with pytest.raises(ValueError):
        swarm.egress_hosts("https://notion.so/", ["a.com", "b.com", "c.com", "d.com"])
    with pytest.raises(ValueError):
        swarm.egress_hosts("https://notion.so/", ["*"])
    with pytest.raises(swarm.BlockedURL):
        swarm.egress_hosts("https://127.0.0.1/terms")


def test_lock_rules_end_in_deny_all_and_refuse_wildcards():
    rules = swarm.lock_rules(["notion.so", "*.notion.so"]).model_dump()["rules"]
    assert rules[-1] == {"action": "deny", "domain": "*"}
    assert [r["domain"] for r in rules if r["action"] == "allow"] == ["notion.so", "*.notion.so"]
    assert swarm.lock_rules([]).model_dump()["rules"] == [{"action": "deny", "domain": "*"}]  # never empty
    for bad in ("*", "*.com.", "evil host"):
        with pytest.raises(ValueError):
            swarm.lock_rules([bad])


def test_unconfigured_worker_is_unavailable(monkeypatch):
    assert swarm.Swarm.from_settings() is None  # tests set no FP_SPRITES_API_BASE
    assert agent.Clients().swarm is None
    sw = make(checkpoints={"scout": "", "advocate": ""})
    assert not sw.available("scout") and not sw.available("advocate")


# --- Job lifecycle ---------------------------------------------------------------------------------

@respx.mock
async def test_job_locks_dns_runs_then_restores(conn):
    routes = mock_api(httpx.Response(200, content=framed({"ok": True, "n": 1})))
    sw = make(conn)
    result = await sw.run_job("scout", {"url": URL}, allow_hosts=["acme.test"], detail="playwright_crawl")
    await sw.aclose()
    assert result == {"ok": True, "n": 1}

    lock, close = (json.loads(c.request.content) for c in routes["policy"].calls)
    assert lock["rules"] == [{"action": "allow", "domain": "acme.test"}, {"action": "deny", "domain": "*"}]
    assert close["rules"] == [{"action": "deny", "domain": "*"}]  # Scout idles with no egress
    assert routes["restore"].call_count == 1
    exec_req = routes["exec"].calls[0].request
    assert exec_req.url.params.get_list("cmd") == [swarm.PYTHON, swarm.RUNNERS["scout"]]
    assert exec_req.url.params["stdin"] == "true" and json.loads(exec_req.content) == {"url": URL}
    assert exec_req.headers["authorization"] == "Bearer tok"

    order = [e.event for e in swarm.events_after(conn)]
    assert order == ["egress_lock", "run", "checkpoint_restore"]
    counts = {(r["name"], r["outcome"]): r["n"] for r in conn.execute("SELECT * FROM metric_counts WHERE kind = 'swarm'")}
    assert counts == {("scout_egress_lock", "ok"): 1, ("scout_restore", "ok"): 1, ("scout_job", "ok"): 1}


@respx.mock
async def test_worker_error_still_restores_the_checkpoint(conn):
    routes = mock_api(httpx.ConnectError("boom"))
    sw = make(conn)
    with pytest.raises(httpx.ConnectError):
        await sw.run_job("scout", {"url": URL}, allow_hosts=["acme.test"])
    await sw.aclose()
    assert routes["policy"].call_count == 2 and routes["restore"].call_count == 1
    events = [(e.event, e.detail) for e in swarm.events_after(conn)]
    assert ("error", "connect_error") in events and events[-1][0] == "checkpoint_restore"
    job = conn.execute("SELECT outcome FROM metric_counts WHERE name = 'scout_job'").fetchone()
    assert job["outcome"] == "error"


@respx.mock
async def test_missing_result_is_an_error_and_restores(conn):
    routes = mock_api(httpx.Response(200, content=b"\x02Traceback: crashed\n\x03\x01"))
    sw = make(conn)
    with pytest.raises(swarm.SwarmError, match="no_result"):
        await sw.run_job("scout", {"url": URL}, allow_hosts=["acme.test"])
    await sw.aclose()
    assert routes["restore"].call_count == 1


@respx.mock
async def test_failed_lock_never_runs_the_job_but_restores(conn):
    routes = mock_api(httpx.Response(200, content=framed({"ok": True})))
    routes["policy"].mock(return_value=httpx.Response(400, json={"error": "bad"}))
    sw = make(conn)
    with pytest.raises(httpx.HTTPStatusError):
        await sw.run_job("scout", {"url": URL}, allow_hosts=["acme.test"])
    await sw.aclose()
    assert not routes["exec"].called and routes["restore"].call_count == 1
    assert conn.execute("SELECT n FROM metric_counts WHERE name = 'scout_egress_lock' AND outcome = 'error'").fetchone()


@respx.mock
async def test_timeout_is_reported_and_restores(conn, monkeypatch):
    async def slow(*a, **kw):
        import asyncio

        await asyncio.sleep(5)

    routes = mock_api(httpx.Response(200, content=b""))
    sw = make(conn)
    monkeypatch.setattr(sw, "exec", slow)
    with pytest.raises(swarm.SwarmError, match="timeout"):
        await sw.run_job("scout", {"url": URL}, allow_hosts=["acme.test"], timeout=0.05)
    await sw.aclose()
    assert routes["restore"].call_count == 1
    assert conn.execute("SELECT outcome FROM metric_counts WHERE name = 'scout_job'").fetchone()["outcome"] == "timeout"


@respx.mock
async def test_restore_stream_error_is_counted_not_raised(conn):
    routes = mock_api(httpx.Response(200, content=framed({"ok": True})))
    routes["restore"].mock(return_value=httpx.Response(200, text='{"type":"error","error":"disk full","time":"t"}\n'))
    sw = make(conn)
    assert await sw.run_job("scout", {}, allow_hosts=["acme.test"]) == {"ok": True}
    await sw.aclose()
    row = conn.execute("SELECT outcome FROM metric_counts WHERE name = 'scout_restore'").fetchone()
    assert row["outcome"] == "error"


@respx.mock
async def test_job_without_hosts_leaves_policy_alone():
    adv = f"{API}/v1/sprites/fp-advocate"
    policy = respx.post(f"{adv}/policy/network").mock(return_value=httpx.Response(200))
    respx.post(f"{adv}/exec").mock(return_value=httpx.Response(200, content=framed({"ok": True})))
    restore = respx.post(f"{adv}/checkpoints/v2/restore").mock(return_value=httpx.Response(200, text=""))
    sw = make()
    assert await sw.run_job("advocate", {"kind": "redline"}, allow_hosts=None) == {"ok": True}
    await sw.aclose()
    assert not policy.called and restore.call_count == 1


async def test_unavailable_role_raises_without_calls():
    sw = make(checkpoints={"scout": "", "advocate": ""})
    with pytest.raises(swarm.SwarmUnavailable):
        await sw.run_job("scout", {}, allow_hosts=["acme.test"])
    await sw.aclose()


def test_decode_exec_accepts_json_shape():
    resp = httpx.Response(200, json={"stdout": scout_runner.encode_result({"a": 1}), "exit_code": 0})
    out = swarm.decode_exec(resp)
    assert out.exit_code == 0 and swarm.extract_result(out.stdout) == {"a": 1}


# --- Scout ------------------------------------------------------------------------------------------

@respx.mock
async def test_scout_refuses_private_targets_before_any_call():
    routes = mock_api(httpx.Response(200, content=framed(ok_result())))
    sw = make()
    with pytest.raises(swarm.BlockedURL):
        await sw.scout("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(swarm.BlockedURL):
        await sw.scout("file:///etc/passwd")
    await sw.aclose()
    assert not any(r.called for r in routes.values())


@respx.mock
async def test_scout_result_outside_the_lock_is_rejected(public_dns):
    mock_api(httpx.Response(200, content=framed(ok_result(final_url="https://evil.example/terms"))))
    sw = make()
    with pytest.raises(swarm.SwarmError, match="outside_lock"):
        await sw.scout(URL)
    await sw.aclose()


@respx.mock
async def test_page_text_uses_scout_before_exa_and_keeps_public_evidence(conn, monkeypatch, public_dns):
    async def empty(url, client=None):
        return "text/html", "<html><body><div id=root></div></body></html>"

    monkeypatch.setattr(agent, "fetch", empty)
    routes = mock_api(httpx.Response(200, content=framed(ok_result(screenshot_png_b64=base64.b64encode(PNG).decode()))))
    exa = respx.post("https://exa.test/contents").mock(return_value=httpx.Response(500))
    cl = agent.Clients()
    cl.swarm, cl.llama = make(conn), None
    text = await agent.page_text(cl, URL, public=swarm.PublicPage(service_id=4242, doc_role="terms"))
    await cl.aclose()

    assert text.startswith("## Terms of Service") and not exa.called
    assert json.loads(routes["exec"].calls[0].request.content)["screenshot"] is True
    rel = swarm.evidence_for(4242, "terms", agent.sha(text))
    assert rel == f"4242/terms/{agent.sha(text)}.png"
    assert (settings.evidence_dir / rel).read_bytes() == PNG


@respx.mock
async def test_private_check_asks_for_no_screenshot_and_keeps_nothing(conn, monkeypatch, public_dns):
    async def refused(url, client=None):
        raise httpx.HTTPStatusError("403", request=httpx.Request("GET", url), response=httpx.Response(403))

    monkeypatch.setattr(agent, "fetch", refused)
    # Even if a worker misbehaved and sent a screenshot back, a private check must not keep it.
    routes = mock_api(httpx.Response(200, content=framed(ok_result(screenshot_png_b64=base64.b64encode(PNG).decode()))))
    before = sorted(settings.evidence_dir.rglob("*")) if settings.evidence_dir.exists() else []
    cl = agent.Clients()
    cl.swarm, cl.llama = make(conn), None
    text = await agent.page_text(cl, "https://acme.test/private-contract")
    await cl.aclose()

    assert "You agree to these terms" in text
    assert json.loads(routes["exec"].calls[0].request.content)["screenshot"] is False
    after = sorted(settings.evidence_dir.rglob("*")) if settings.evidence_dir.exists() else []
    assert after == before


@respx.mock
async def test_scout_failure_falls_back_to_exa(conn, monkeypatch, public_dns):
    async def empty(url, client=None):
        return "text/html", "<html></html>"

    monkeypatch.setattr(agent, "fetch", empty)
    routes = mock_api(httpx.Response(200, content=framed({"ok": False, "error": "timeout_error"})))
    respx.post("https://exa.test/contents").mock(return_value=httpx.Response(200, json={"results": [
        {"url": URL, "text": "## Terms\n" + "Exa says you agree. " * 40}]}))
    cl = agent.Clients()
    cl.swarm, cl.llama = make(conn), None
    text = await agent.page_text(cl, URL)
    await cl.aclose()
    assert "Exa says you agree" in text and routes["restore"].call_count == 1


# --- Evidence ---------------------------------------------------------------------------------------

def test_evidence_rejects_bad_keys_and_non_png(tmp_path):
    page = swarm.PublicPage(service_id=1)
    sha = "a" * 64
    assert swarm.save_evidence(page, sha, base64.b64encode(b"<svg/>").decode(), root=tmp_path) is None
    assert swarm.save_evidence(page, "../../etc", base64.b64encode(PNG).decode(), root=tmp_path) is None
    assert swarm.save_evidence(swarm.PublicPage(service_id=1, doc_role="../x"), sha, base64.b64encode(PNG).decode(), root=tmp_path) is None
    assert swarm.save_evidence(page, sha, "", root=tmp_path) is None
    assert swarm.save_evidence(page, sha, base64.b64encode(PNG).decode(), root=tmp_path) == f"1/terms/{sha}.png"
    assert swarm.evidence_for(1, "terms", sha, root=tmp_path) == f"1/terms/{sha}.png"
    assert swarm.evidence_for(1, "privacy", sha, root=tmp_path) is None


# --- Telemetry --------------------------------------------------------------------------------------

def test_events_after_pages_by_id(conn):
    for event in ("egress_lock", "run", "checkpoint_restore"):
        metrics.swarm_event(conn, "scout", event, "playwright_crawl", 12)
    first = swarm.events_after(conn, 0, limit=2)
    assert [e.event for e in first] == ["egress_lock", "run"]
    rest = swarm.events_after(conn, first[-1].id)
    assert [e.event for e in rest] == ["checkpoint_restore"] and rest[0].duration_ms == 12
    assert [e.event for e in swarm.recent_events(conn, 2)] == ["run", "checkpoint_restore"]

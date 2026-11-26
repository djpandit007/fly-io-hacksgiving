"""Owner metrics: storage, recording at each point, provider-reported cost, and the /stats report."""

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import (
    agent,
    bot,
    db,
    exa,
    judge,
    main,
    memory,
    metrics,
    notify,
    parse,
    users,
    writer,
)
from app.config import settings
from app.gateway import Gateway
from app.telegram import Incoming, Telegram, TelegramError
from tests.test_bot import FakeTelegram as BotFakeTelegram
from tests.test_judge import GOOD


class Seen:
    """A recorder that keeps what it was told."""

    def __init__(self):
        self.counts: list[tuple[str, str, str]] = []
        self.amounts: dict[tuple[str, str], float] = {}

    def count(self, kind, name, outcome="ok", ms=0):
        self.counts.append((kind, name, outcome))

    def usage(self, provider, unit, amount):
        self.amounts[(provider, unit)] = self.amounts.get((provider, unit), 0) + amount


class FakeTelegram(BotFakeTelegram):
    def __init__(self, broken: int | None = None):
        super().__init__()
        self.broken = broken

    async def send(self, chat, text, **kw):
        if chat == self.broken:
            raise TelegramError("Telegram sendMessage failed: blocked")
        return await super().send(chat, text, **kw)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "metrics.db")


def counts(conn, kind):
    return {(r["name"], r["outcome"]): r["n"] for r in conn.execute("SELECT * FROM metric_counts WHERE kind = ?", (kind,))}


def everything_stored(conn) -> str:
    tables = ("metric_runs", "metric_failures", "metric_counts", "metric_usage")
    return " ".join(str(tuple(r)) for t in tables for r in conn.execute(f"SELECT * FROM {t}"))


# --- Storage ----------------------------------------------------------------------------------------

def test_counters_add_up_per_day_and_keep_the_slowest(conn):
    metrics.count(conn, "provider", "exa", "ok", 100)
    metrics.count(conn, "provider", "exa", "ok", 300)
    metrics.count(conn, "provider", "exa", "error", 50)
    [ok] = conn.execute("SELECT * FROM metric_counts WHERE outcome = 'ok'").fetchall()
    assert (ok["n"], ok["total_ms"], ok["max_ms"]) == (2, 400, 300)
    assert counts(conn, "provider")[("exa", "error")] == 1


def test_usage_adds_up_per_provider_and_unit(conn):
    metrics.usage(conn, "exa", "usd", 0.007)
    metrics.usage(conn, "exa", "usd", 0.001)
    metrics.usage(conn, "llama", "credits", 51)
    amounts = {(r["provider"], r["unit"]): r["amount"] for r in conn.execute("SELECT * FROM metric_usage")}
    assert amounts == {("exa", "usd"): pytest.approx(0.008), ("llama", "credits"): 51}


def test_recording_on_a_closed_connection_does_not_raise(conn):
    conn.close()
    metrics.count(conn, "provider", "exa")
    metrics.usage(conn, "exa", "usd", 1)
    metrics.failure(conn, "mail", ValueError("x"))
    metrics.run(conn, duration_ms=1, checked=0, failed=[], changes=0, nudges=0, sent=0, undelivered=0)
    metrics.prune(conn)


def test_prune_removes_only_metrics_older_than_90_days(conn):
    old = datetime.now(UTC) - timedelta(days=91)
    conn.execute("INSERT INTO metric_counts (day, kind, name, outcome, n) VALUES (?, 'provider', 'exa', 'ok', 1)", (old.date().isoformat(),))
    conn.execute("INSERT INTO metric_usage (day, provider, unit, amount) VALUES (?, 'exa', 'usd', 1)", (old.date().isoformat(),))
    conn.execute("INSERT INTO metric_failures (at, kind, error) VALUES (?, 'mail', 'X')", (old.isoformat(timespec="seconds"),))
    conn.execute(
        "INSERT INTO metric_runs (ran_at, duration_ms, checked, failed, changes, nudges, sent, undelivered) "
        "VALUES (?, 1, 1, '[]', 0, 0, 0, 0)", (old.isoformat(timespec="seconds"),),
    )
    metrics.count(conn, "provider", "exa")
    metrics.usage(conn, "exa", "usd", 2)
    metrics.failure(conn, "mail", ValueError("x"))
    metrics.run(conn, duration_ms=1, checked=1, failed=[], changes=0, nudges=0, sent=0, undelivered=0)
    metrics.prune(conn)
    for table in ("metric_counts", "metric_usage", "metric_failures", "metric_runs"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1, table


# --- Provider calls -----------------------------------------------------------------------------------

@respx.mock
async def test_gateway_counts_a_retry_then_success(monkeypatch):
    monkeypatch.setattr("app.gateway.asyncio.sleep", lambda *_: _nothing())
    respx.post("https://exa.test/search").mock(side_effect=[httpx.Response(503), httpx.Response(200, json={})])
    seen = Seen()
    async with Gateway(settings.exa, recorder=seen) as gw:
        await gw.post("search", json={})
    assert seen.counts == [("provider", "exa", "retried"), ("provider", "exa", "ok")]


async def _nothing():
    return None


@respx.mock
async def test_gateway_counts_a_final_failure_and_still_raises():
    respx.post("https://exa.test/search").mock(return_value=httpx.Response(400, text="bad"))
    seen = Seen()
    async with Gateway(settings.exa, recorder=seen) as gw:
        with pytest.raises(httpx.HTTPStatusError):
            await gw.post("search", json={})
    assert seen.counts == [("provider", "exa", "error")]


@respx.mock
async def test_gateway_without_a_recorder_still_works():
    respx.post("https://exa.test/search").mock(return_value=httpx.Response(200, json={"costDollars": {"total": 1}}))
    async with Gateway(settings.exa) as gw:
        assert await exa.search(gw, "notion terms") == []


@respx.mock
async def test_telegram_calls_and_failures_are_counted_as_a_provider():
    respx.post(url__regex=r"https://api\.telegram\.org/bot.*/sendMessage").mock(
        side_effect=[httpx.Response(200, json={"ok": True, "result": {}}), httpx.Response(403, json={"ok": False, "description": "blocked"})]
    )
    tg = Telegram(token="t")
    tg.recorder = seen = Seen()
    await tg.send(1, "hi")
    with pytest.raises(TelegramError):
        await tg.send(1, "hi")
    await tg.aclose()
    assert seen.counts == [("provider", "telegram", "ok"), ("provider", "telegram", "error")]


# --- Provider-reported cost ---------------------------------------------------------------------------

def chat_reply(usage):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": usage})


@respx.mock
async def test_writer_records_the_cost_opencode_reports():
    respx.post("https://opencode.test/inference/openai/v1/chat/completions").mock(
        side_effect=[chat_reply({"prompt_tokens": 33, "completion_tokens": 17, "cost": 0.00001212}), chat_reply({"completion_tokens": 5})]
    )
    seen = Seen()
    async with Gateway(settings.opencode, recorder=seen) as gw:
        await writer.chat(gw, "hi")
        assert seen.amounts == {("opencode", "usd"): 0.00001212, ("opencode", "input_tokens"): 33, ("opencode", "output_tokens"): 17}
        await writer.chat(gw, "hi")  # no cost reported: nothing is added to the dollar total
    assert seen.amounts[("opencode", "usd")] == 0.00001212


@respx.mock
async def test_judge_records_tokens_and_cost_when_reported():
    route = respx.post("https://opencode.test/inference/systemone/v1/systemone")
    costed = {**GOOD, "usage": {"input_tokens": 284, "output_tokens": 20, "cost": 0.00001193}}
    route.mock(side_effect=[httpx.Response(200, json=costed), httpx.Response(200, json=GOOD)])
    change = judge.SectionChange(heading="Disputes", kind="modified", before="a", after="b")
    seen = Seen()
    async with Gateway(settings.opencode, recorder=seen) as gw:
        assert (await judge.judge(gw, "Acme", change)).category == "arbitration"
        assert seen.amounts == {("opencode", "usd"): 0.00001193, ("opencode", "input_tokens"): 284, ("opencode", "output_tokens"): 20}
        await judge.judge(gw, "Acme", change)  # tokens but no cost
    assert seen.amounts[("opencode", "usd")] == 0.00001193 and seen.amounts[("opencode", "input_tokens")] == 284 + 900


@respx.mock
async def test_exa_records_cost_dollars_and_tolerates_other_shapes():
    respx.post("https://exa.test/search").mock(
        side_effect=[
            httpx.Response(200, json={"results": [], "costDollars": {"total": 0.007, "search": {"neural": 0.007}}}),
            httpx.Response(200, json={"results": []}),
            httpx.Response(200, json={"results": [], "costDollars": "free"}),
        ]
    )
    respx.post("https://exa.test/contents").mock(
        return_value=httpx.Response(200, json={"results": [{"url": "https://x.test", "text": "T"}], "costDollars": {"total": 0.001}})
    )
    seen = Seen()
    async with Gateway(settings.exa, recorder=seen) as gw:
        for _ in range(3):
            await exa.search(gw, "q")
        assert await exa.contents(gw, "https://x.test") == "T"
    assert seen.amounts == {("exa", "usd"): pytest.approx(0.008)}


@respx.mock
async def test_llama_parse_asks_for_usage_and_records_credits_when_billed():
    base = "https://api.cloud.llamaindex.ai/api/v2/parse"
    respx.post(base).mock(return_value=httpx.Response(200, json={"id": "j1", "status": "PENDING"}))
    job = {"id": "j1", "status": "COMPLETED"}
    page = {"markdown": {"pages": [{"page_number": 1, "markdown": "# T"}]}}
    get = respx.get(f"{base}/j1").mock(
        side_effect=[
            httpx.Response(200, json={"job": job}),
            httpx.Response(200, json={"job": {**job, "usage": {"credits": 51.0}}, **page}),
            httpx.Response(200, json={"job": job}),
            httpx.Response(200, json={"job": {**job, "usage": {"credits": None}}, **page}),
        ]
    )
    seen = Seen()
    async with Gateway(settings.llama, recorder=seen) as gw:
        assert (await parse.parse_url(gw, "https://x.test/a.pdf")).markdown == "# T"
        assert seen.amounts == {("llama", "credits"): 51.0}
        await parse.parse_url(gw, "https://x.test/a.pdf")  # not billed yet: nothing recorded, parse unaffected
    assert seen.amounts == {("llama", "credits"): 51.0}
    assert get.calls[1].request.url.params.get_list("expand") == ["markdown", "usage"]


@respx.mock
async def test_pinecone_records_embedding_tokens_and_read_units():
    respx.post("https://pinecone.test/embed").mock(
        side_effect=[
            httpx.Response(200, json={"data": [{"values": [0.1]}], "usage": {"total_tokens": 4}}),
            httpx.Response(200, json={"data": [{"values": [0.1]}]}),
        ]
    )
    respx.post(f"https://pinecone.test/indexes/{settings.pinecone_index}/query").mock(
        side_effect=[
            httpx.Response(200, json={"matches": [], "usage": {"readUnits": 1, "egressBytes": 28}}),
            httpx.Response(200, json={"matches": []}),
        ]
    )
    seen = Seen()
    async with Gateway(settings.pinecone, recorder=seen) as gw:
        for _ in range(2):
            await memory.similar(gw, "clause", exclude_service="A", services=["A", "B"])
    assert seen.amounts == {("pinecone", "embed_tokens"): 4, ("pinecone", "read_units"): 1}


# --- Runs, deliveries and jobs ------------------------------------------------------------------------

async def test_a_failed_delivery_is_counted_and_the_others_still_arrive(conn):
    a = users.join_member(conn, "join-code", chat_id=1)
    b = users.join_member(conn, "join-code", chat_id=2)
    tg = FakeTelegram(broken=1)
    delivered = await notify.deliver(conn, tg, None, {a.id: ["one"], b.id: ["two"]}, "Subject")
    assert delivered == (1, 1) and tg.to(2) == ["two"]


def test_tick_records_the_run_and_names_the_failed_service(monkeypatch):
    from fastapi.testclient import TestClient

    async def fake_tick(conn, cl):
        return agent.TickResult(checked=2, failed=["Notion"], changes=1, nudges=0)

    monkeypatch.setattr(agent, "tick", fake_tick)
    with TestClient(main.app) as client:
        before = _runs()
        resp = client.post("/tick", headers={"X-Fine-Print-Secret": "test-secret"})
        assert resp.json()["failed"] == ["Notion"] and resp.json()["undelivered"] == 0
        runs = _runs()
    assert len(runs) == len(before) + 1
    assert (runs[-1]["checked"], runs[-1]["failed"], runs[-1]["changes"]) == (2, '["Notion"]', 1)
    assert runs[-1]["duration_ms"] >= 0


def _runs():
    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM metric_runs ORDER BY id").fetchall()


async def test_a_failed_mail_job_is_recorded_without_sender_or_content(conn, monkeypatch):
    async def boom(*args):
        raise TimeoutError("could not read the lease from alice@example.com")

    monkeypatch.setattr(main.intake, "process", boom)
    app_ = SimpleNamespace(state=SimpleNamespace(conn=conn, clients=None, tg=None))
    await main._handle_mail(app_, "<msg-1@example.com>", None, "alice@example.com", "mail-test")
    [row] = conn.execute("SELECT kind, error FROM metric_failures").fetchall()
    assert tuple(row) == ("mail", "TimeoutError")
    assert counts(conn, "job") == {("mail", "error"): 1}
    assert "alice" not in everything_stored(conn) and "lease" not in everything_stored(conn)


# --- Commands and /stats ------------------------------------------------------------------------------

async def say(conn, tg, chat, text, **kwargs):
    await bot.handle(conn, None, tg, Incoming(chat_id=chat, name="Someone", text=text, **kwargs))


async def test_commands_are_counted_by_name_without_content_or_member(conn, monkeypatch):
    async def fake_check(cl, url, **kw):
        return "card"

    async def broken_file(*args, **kw):
        raise RuntimeError("cannot parse secret-lease.pdf")

    monkeypatch.setattr(agent, "check_url", fake_check)
    monkeypatch.setattr(agent, "intake_file", broken_file)
    tg = FakeTelegram()
    tg.download = lambda file_id: _file()
    await say(conn, tg, 4242, "/start join-code")
    await say(conn, tg, 4242, "/list")
    await say(conn, tg, 4242, "https://private.example/terms?token=abc")
    await say(conn, tg, 4242, "my password is hunter2")
    await say(conn, tg, 4242, "", file_id="f1", file_name="secret-lease.pdf")
    assert counts(conn, "command") == {
        ("/start", "ok"): 1, ("/list", "ok"): 1, ("check:url", "ok"): 1, ("other", "ok"): 1, ("check:file", "error"): 1,
    }
    stored = everything_stored(conn)
    for private in ("private.example", "hunter2", "secret-lease", "4242", "Someone"):
        assert private not in stored


async def _file():
    return "secret-lease.pdf", b"%PDF"


async def test_stats_is_owner_only(conn):
    tg = FakeTelegram()
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/stats")
    assert tg.to(2)[-1] == "That command is owner-only."
    await say(conn, tg, 1, "/stats")
    assert "last 7 days" in tg.to(1)[-1]
    assert "/stats" in bot.help_text(users.owner(conn)) and "/stats" not in bot.help_text(users.by_chat(conn, 2))


def test_report_covers_runs_providers_cost_and_usage(conn):
    users.join_member(conn, "join-code", chat_id=77, name="Private Person")
    metrics.run(conn, duration_ms=42_000, checked=12, failed=["Notion"], changes=3, nudges=1, sent=4, undelivered=1)
    metrics.failure(conn, "mail", TimeoutError())
    metrics.count(conn, "provider", "opencode", "ok", 800)
    metrics.count(conn, "provider", "opencode", "error", 1200)
    metrics.count(conn, "provider", "agentmail", "ok", 90)
    metrics.count(conn, "command", "/watch")
    metrics.usage(conn, "opencode", "usd", 0.03)
    metrics.usage(conn, "exa", "usd", 0.008)
    metrics.usage(conn, "llama", "credits", 136)
    metrics.usage(conn, "pinecone", "read_units", 12)
    text = metrics.report(conn)
    assert "12 checked, <b>1 failed</b> (Notion), 3 changes, 1 nudges, 4 sent, <b>1 undelivered</b>, 42.0 s" in text
    assert "mail TimeoutError ×1" in text
    assert "opencode — 2 calls, 1 failed, 0 retried, avg 1.0 s, slowest 1.2 s" in text
    assert "<b>$0.0380</b> reported by providers" in text
    assert "Not included in the total: llama 136 credits; pinecone 12 read units" in text
    assert "Not reported by: agentmail" in text
    assert "The cost of running the Sprite is not included." in text
    assert "Members: 1 active, 1 joined" in text and "/watch 1" in text
    assert "Private Person" not in text and "77" not in text


def test_report_on_an_empty_database(conn):
    text = metrics.report(conn)
    assert "None recorded." in text and "No dollar cost reported." in text and "Commands: none" in text


# --- Buttons and answer feedback in /stats -------------------------------------------------------------

def test_report_shows_button_use_and_answer_feedback(conn):
    from app import feedback
    from app.outgoing import Answer

    metrics.count(conn, "tap", "unwatch")
    metrics.count(conn, "tap", "unwatch")
    metrics.count(conn, "tap", "explain", "error")
    for vote, reason in (("up", None), ("up", None), ("down", "wrong")):
        token = feedback.issue(conn, Answer(kind="verdict", model="m", prompt_version="v2"))
        feedback.vote(conn, token, vote)
        if reason:
            feedback.reason(conn, token, reason)
    text = metrics.report(conn)
    assert "Buttons: unwatch 2, explain 1 (1 failed)" in text
    assert "<b>Answer feedback</b>\n• Verdict cards — 3 rated, 66% 👍; 👎 because: Wrong 1 (by prompt: v2 66% of 3)" in text


def test_report_without_feedback_says_so(conn):
    text = metrics.report(conn)
    assert "Buttons: none" in text and "No feedback yet." in text and "0%" not in text


def test_daily_run_prunes_old_feedback(monkeypatch):
    from fastapi.testclient import TestClient

    from app import feedback
    from app.outgoing import Answer

    async def fake_tick(conn_, cl):
        return agent.TickResult()

    monkeypatch.setattr(agent, "tick", fake_tick)
    with TestClient(main.app) as client:
        conn = db.connect(settings.db_path)
        old = feedback.issue(conn, Answer(kind="alert", model="m", prompt_version="v1"))
        conn.execute("UPDATE answers SET sent_at = ? WHERE token = ?",
                     ((datetime.now(UTC) - timedelta(days=100)).isoformat(), old))
        client.post("/tick", headers={"X-Fine-Print-Secret": "test-secret"})
        assert conn.execute("SELECT COUNT(*) FROM answers WHERE token = ?", (old,)).fetchone()[0] == 0

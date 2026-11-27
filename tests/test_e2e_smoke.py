"""End-to-end local smoke test (tasks.md 6.2): one member's day with Fine Print, driven through the real
entry points, with only the external boundaries faked.

Flow: two members join -> A sets /jurisdiction US-CA and watches a service whose Policy Bundle has Terms
and AI Terms; B watches another service -> a daily check finds the AI Terms now contradict the Terms
(stored once, alerted to A) -> A sends a personal employment contract (domain playbook verdict, statutory
note, ephemeral conflict check against A's watched service; B gets nothing) -> a later check finds an
arbitration change with an email opt-out -> A drafts, fills in a missing field, approves (one receipt;
a replayed approval sends nothing) -> A asks for a Balanced redline kit (a real tracked-changes .docx
built by the Advocate runner's builder) -> A signs in to the Control Room with a /dashboard magic link
(only A's data; B's session can't open A's change) while the swarm telemetry stream shows the Advocate's
jobs, sanitized -> every SQLite row is checked for the contract's and the notice's text.

Entry points: `bot.handle` (messages and taps, with a fake Telegram), `agent.tick` + `notify.deliver`
(what POST /tick runs), FastAPI `TestClient` for /auth/magic, /room and /changes/{id}, and
`web.swarm_stream` (the generator behind GET /api/swarm/stream; an endless SSE response is not
practical to read through TestClient).

Faked boundaries: Jev and the OpenCode writer (respx), Pinecone (respx, recorded), LlamaParse /
LlamaExtract (respx), the opt-out extraction model (pydantic-ai FunctionModel), page fetching and
companion discovery (`agent.page_text`, `discover.discover_companions`, `agent.backfill`,
`tosdr.lookup`: the same seams tests/test_bundles.py uses), and the Advocate Sprite (`Swarm.run_job`:
scripted results, but the kit job runs `workers.advocate_runner.kit` locally to build a real .docx).
"""

from __future__ import annotations

import copy
import io
import json
import re
import zipfile
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app import advocate, agent, bot, db, metrics, notify, sessions, web
from app.advocate import OptOut, OptOutExtract
from app.config import Provider, settings
from app.gateway import Gateway
from app.main import app
from app.telegram import Incoming
from tests.test_advocate import FakeSwarm, FakeTelegram
from tests.test_bundles import AI_NEW, AI_OLD, TERMS
from tests.test_judge import GOOD
from tests.test_playbooks import OFFER, QUOTES
from tests.test_web import _Disconnect
from workers import advocate_runner as ar

JEV = "https://opencode.test/inference/systemone/v1/systemone"
CHAT = "https://opencode.test/inference/openai/v1/chat/completions"
EMBED = "https://pinecone.test/embed"
QUERY = "https://pinecone.test/indexes/fine-print/query"
UPSERT = "https://pinecone.test/indexes/fine-print/vectors/upsert"
LLAMA = "https://llama.test"

A_CHAT, B_CHAT = 61_001, 61_002
TERMS_URL = "https://smokeco.test/terms"
AI_URL = "https://smokeco.test/ai-terms"
OTHER_URL = "https://otherco.test/terms"
OTHER_TERMS = "## Use\nOtherco lets you store files.\n## Fees\nOtherco bills yearly."

# The member's private contract: an offer letter with a non-compete, an IP grab and a ban on letting
# company information train AI models, plus a reference string that must never reach SQLite or Pinecone.
CONTRACT_MARK = "QUOKKA-CONTRACT-5521"
CONTRACT_CLAUSE = QUOTES["non_compete_clause"]  # only ever in the contract
CONTRACT = OFFER + f"\n## Reference\nEmployee reference {CONTRACT_MARK}, kept confidential.\n"
CONTRACT_TITLE = "Offer Letter and PIIA"
FOUND = ("side_project_ip_capture", "non_compete_clause")

# The arbitration change with an email opt-out on the service's own domain.
INSTRUCTION = "You may opt out of arbitration by emailing arbitration-optout@smokeco.test within 30 days of this change."
DISPUTES = f"## 3. Disputes\nAll disputes will be resolved by binding individual arbitration. {INSTRUCTION}"
NOTICE_MARK = "WOMBAT-ACCT-8842"  # what A types as their account username: goes into the notice body only
NOTICE_SENTENCE = "Please treat this message as my formal notice"
LEAKY_URL, LEAKY_EMAIL = "https://smokeco.test/private?u=1", "ada.private@example.com"


class Telegram(FakeTelegram):
    """tests/test_advocate.py's fake Telegram, plus file downloads."""

    async def download(self, file_id):
        assert file_id == "contract-file"
        return "offer.pdf", b"%PDF-1.7 fake scan"

    def to(self, chat) -> list[dict]:
        return [m for m in self.messages if m["chat"] == chat]


class Advocate(FakeSwarm):
    """tests/test_advocate.py's fake Advocate, recording telemetry the way `Swarm.run_job` does."""

    def __init__(self, conn, results):
        super().__init__(results)
        self.conn = conn

    async def run_job(self, role, payload, *, allow_hosts, detail="", args=(), timeout=None):
        metrics.swarm_event(self.conn, role, "egress_lock", "dns_locked")
        metrics.swarm_event(self.conn, role, "run", detail)
        if payload["kind"] == "email":  # a careless label from a worker: the stream must still scrub it
            self.conn.execute(
                "INSERT INTO swarm_events (at, sprite_role, event, detail, duration_ms) VALUES (?, 'advocate', 'run', ?, 5)",
                (db.now(), f"sent to {LEAKY_EMAIL} via {LEAKY_URL}"),
            )
        try:
            return await super().run_job(role, payload, allow_hosts=allow_hosts, detail=detail, args=args, timeout=timeout)
        finally:
            metrics.swarm_event(self.conn, role, "checkpoint_restore", "base", 300)


class Jev:
    """Answers each System One request by its kind, like the fakes in test_bundles and test_playbooks."""

    def __init__(self):
        self.opt_out = 0.1       # change judgments: no opt-out until the arbitration change
        self.contradicts = 0.92  # contradiction batches
        self.bodies: list[dict] = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        questions, state = body["questions"], body["state"]
        if "material" in questions:  # a changed section of a watched service
            answers = copy.deepcopy(GOOD["answers"])
            answers["opt_out"]["noul"] = self.opt_out
            return httpx.Response(200, json={"model": "jev-1.14", "answers": answers})
        scores = [k for k, q in questions.items() if q["type"] == "score" and re.fullmatch(r"s\d+", k)]
        out = {}
        for key in questions:
            if key.startswith("contradicts_"):
                out[key] = {"type": "noul", "noul": self.contradicts}
            elif key.startswith("severity_"):
                out[key] = {"type": "score", "score": 2.7, "confidence": 0.7, "probabilities": {}}
            elif key == "domain":
                out[key] = {"type": "choice", "choice": "employment_or_nda", "confidence": 0.9,
                            "probabilities": {"employment_or_nda": 0.9}}
            elif key in scores:  # clause severity: originals first, then proposed rewrites
                out[key] = {"type": "score", "score": 2.8 if scores.index(key) < max(1, len(scores) // 2) else 0.8,
                            "confidence": 0.9, "probabilities": {}}
            elif re.fullmatch(r"k\d+", key):  # cross-contract conflicts
                out[key] = {"type": "noul", "noul": 0.9}
            elif "jurisdiction" in state:  # statutory confirmations
                out[key] = {"type": "noul", "noul": 0.95}
            else:  # the domain's red-flag battery
                out[key] = {"type": "noul", "noul": 0.9 if key in FOUND else 0.05}
        return httpx.Response(200, json={"model": "jev-1.14", "answers": out})


def writer(request):
    """The verdict card for the contract (with verbatim QUOTE lines); a one-line summary for anything else."""
    if CONTRACT_MARK in request.content.decode():
        content = "PUSH BACK\n- The non-compete and IP clauses go further than usual.\n" + "\n".join(
            f"QUOTE {k}: {QUOTES[k]}" for k in FOUND)
    else:
        content = "Smokeco changed how it may use your content."
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def mock_providers(jev: Jev) -> dict[str, respx.Route]:
    parsed = {"job": {"id": "pj-1", "status": "COMPLETED"},
              "markdown": {"pages": [{"page_number": 1, "markdown": CONTRACT}]}}
    extracted = {"id": "ex-1", "status": "COMPLETED", "extract_result": {"document_title": CONTRACT_TITLE, "deadlines": []}}
    return {
        "jev": respx.post(JEV).mock(side_effect=jev),
        "chat": respx.post(CHAT).mock(side_effect=writer),
        "embed": respx.post(EMBED).mock(return_value=httpx.Response(200, json={"data": [{"values": [0.1] * 1024}]})),
        "query": respx.post(QUERY).mock(return_value=httpx.Response(200, json={"matches": []})),
        "upsert": respx.post(UPSERT).mock(return_value=httpx.Response(200, json={"upsertedCount": 1})),
        "parse": respx.post(f"{LLAMA}/api/v2/parse/upload").mock(
            return_value=httpx.Response(200, json={"id": "pj-1", "status": "PENDING"})),
        "parsed": respx.get(url__startswith=f"{LLAMA}/api/v2/parse/pj-1").mock(return_value=httpx.Response(200, json=parsed)),
        "extract": respx.post(url__startswith=f"{LLAMA}/api/v2/extract").mock(
            return_value=httpx.Response(200, json={"id": "ex-1", "status": "PENDING"})),
        "extracted": respx.get(url__startswith=f"{LLAMA}/api/v2/extract/ex-1").mock(
            return_value=httpx.Response(200, json=extracted)),
    }


@pytest.fixture
def pages(monkeypatch):
    """Live pages by URL (tests edit them between checks); no archive history; no community grade."""
    live = {TERMS_URL: TERMS, AI_URL: AI_OLD, OTHER_URL: OTHER_TERMS}

    async def page_text(cl, url, public=None):
        return live[url]

    async def backfill(name, url, days=365, document="Terms of Service"):
        return []

    async def companions(terms_url, client=None):
        return {"ai_terms": AI_URL} if terms_url == TERMS_URL else {}

    async def no_grade(q, client=None):
        return None

    monkeypatch.setattr(agent, "page_text", page_text)
    monkeypatch.setattr(agent, "backfill", backfill)
    monkeypatch.setattr(agent.discover, "discover_companions", companions)
    monkeypatch.setattr(agent.tosdr, "lookup", no_grade)
    return live


@pytest.fixture(autouse=True)
def _fresh_caches():
    for cache in (advocate.OFFERS, advocate.PENDING_FIELDS, advocate.DRAFTS, advocate.CONTRACTS, advocate.REDLINE_NEXT):
        cache.clear()
    yield
    for cache in (advocate.OFFERS, advocate.PENDING_FIELDS, advocate.DRAFTS, advocate.CONTRACTS, advocate.REDLINE_NEXT):
        cache.clear()


@pytest.fixture
def web_app(tmp_path, monkeypatch):
    """The FastAPI app on a fresh database file: the server thread gets its own connection to it
    (SQLite connections stay on their thread), the test gets another. Sign-in links point at fp.test."""
    monkeypatch.setattr(sessions, "settings", settings.model_copy(update={"public_url": "https://fp.test"}))
    path = tmp_path / "smoke.db"
    conn = db.connect(path)
    with TestClient(app, base_url="https://testserver") as client:
        original = app.state.conn
        app.state.conn = client.portal.call(db.connect, path)
        try:
            yield SimpleNamespace(client=client, conn=conn)
        finally:
            client.portal.call(app.state.conn.close)
            app.state.conn = original
    conn.close()


def extraction(opt: OptOut):
    def fn(messages, info: AgentInfo) -> ModelResponse:
        value = OptOutExtract(found=True, opt_out=opt)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, value.model_dump(mode="json"))])

    return advocate.extractor.override(model=FunctionModel(fn))


def counterparty(job):
    assert CONTRACT_MARK in job["document"] and {c["flag"] for c in job["clauses"]} == set(FOUND)
    return {"ok": True, "items": [
        {"flag": "non_compete_clause", "quote": QUOTES["non_compete_clause"], "exploit": "Block any job in the industry."},
        {"flag": "side_project_ip_capture", "quote": QUOTES["side_project_ip_capture"], "exploit": "Claim weekend apps."},
    ]}


def consumer(job):
    assert "document" not in job and job["tone"] == "balanced"
    return {"ok": True, "items": [
        {"flag": "non_compete_clause", "replacement": "Employee shall not solicit the Company's clients for six months.",
         "rationale": "Non-competes are void in California; a narrow non-solicit is the usual fallback."},
        {"flag": "side_project_ip_capture",
         "replacement": "Employee assigns inventions made with Company resources or relating to its business.",
         "rationale": "Carves out personal projects."},
    ], "email": {"subject": "Offer: two clause changes", "body": "Could we narrow the non-compete and IP clauses?"}}


def dump(conn) -> str:
    """Every row of every table, every column, as text."""
    out = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        out += [" | ".join(str(v) for v in tuple(r)) for r in conn.execute(f"SELECT * FROM {table}")]
    return "\n".join(out)


def buttons_of(message: dict) -> list[tuple[str, str]]:
    return [b for row in (message["buttons"] or []) for b in row]


def tapped(message: dict, prefix: str) -> str:
    [data] = [d for _, d in buttons_of(message) if d.startswith(prefix)]
    return data


def magic_token(message: dict) -> str:
    m = re.search(r'href="https://fp\.test/auth/magic\?token=([^"]+)"', message["text"])
    assert m, message["text"]
    return m.group(1)


@respx.mock
async def test_end_to_end_smoke(web_app, pages, monkeypatch):
    conn, client = web_app.conn, web_app.client
    jev = Jev()
    routes = mock_providers(jev)
    monkeypatch.setattr(ar.shutil, "which", lambda name: None)  # no LibreOffice here: .docx only
    tg = Telegram()
    cl = agent.Clients()
    cl.llama = Gateway(Provider(name="llama", base_url=LLAMA, key="llama-key"))
    sw = Advocate(conn, {"email": {"ok": True, "message_id": "<optout-1@agentmail.to>"},
                         "counterparty": counterparty, "consumer": consumer, "kit": ar.kit})
    cl.swarm = sw

    async def say(chat, text="", name="Ada Lovelace", **kw):
        await bot.handle(conn, cl, tg, Incoming(chat_id=chat, name=name, text=text, message_id=len(tg.messages) + 1, **kw))

    async def tap(chat, message, data, name="Ada Lovelace"):
        await bot.handle(conn, cl, tg, Incoming(chat_id=chat, name=name, kind="tap", data=data, callback_id="cb",
                                                message_id=message.get("id", 7), buttons=message["buttons"] or []))

    # --- 1. Two members join; A watches a Policy Bundle; the AI Terms come to contradict the Terms ---------
    await say(A_CHAT, "/start join-code")
    await say(B_CHAT, "/start join-code", name="Bob")
    a, b = (conn.execute("SELECT * FROM users WHERE chat_id = ?", (c,)).fetchone() for c in (A_CHAT, B_CHAT))
    assert a["status"] == b["status"] == "active" and a["id"] != b["id"]
    await say(A_CHAT, "/jurisdiction US-CA")
    assert "Saved <b>US-CA</b>" in tg.texts(A_CHAT)[-1]
    await say(A_CHAT, f"/watch {TERMS_URL}")
    assert "Watching <b>Smokeco</b>" in tg.texts(A_CHAT)[-1]
    assert "📄 Documents watched: Terms, AI Terms" in tg.texts(A_CHAT)[-1]
    await say(B_CHAT, f"/watch {OTHER_URL}", name="Bob")
    assert "Watching <b>Otherco</b>" in tg.texts(B_CHAT)[-1]
    svc = agent.find_service(conn, TERMS_URL)
    terms_doc, ai_doc = agent.documents_for(conn, svc)
    assert (terms_doc.role, ai_doc.role) == ("terms", "ai_terms")

    pages[AI_URL] = AI_NEW
    b_seen = len(tg.to(B_CHAT))
    result = await agent.tick(conn, cl)
    await notify.deliver(conn, tg, cl, result.deliveries, "Fine Print: changes to terms you accepted")
    assert result.checked == 2 and result.failed == [] and result.contradictions == 1
    [row] = conn.execute("SELECT * FROM document_contradictions").fetchall()
    assert (row["service_id"], row["doc_a_id"], row["doc_b_id"]) == (svc.id, terms_doc.id, ai_doc.id)
    [warning] = [t for t in tg.texts(A_CHAT) if "contradict each other" in t]
    assert warning.startswith("⚠️ <b>Smokeco</b>: its Terms and AI Terms contradict each other")
    assert "<b>Terms · 1. Your Content</b>\n“We never use your content" in warning
    assert "<b>AI Terms · Model Training</b>\n“You grant us a perpetual" in warning
    assert len(tg.to(B_CHAT)) == b_seen  # B doesn't watch Smokeco

    # --- 2. A sends a personal contract: domain playbook, statutory note, private conflict check --------
    upserts_before = routes["upsert"].call_count
    await say(A_CHAT, file_id="contract-file", file_name="offer.pdf")
    card = tg.to(A_CHAT)[-1]
    text = card["text"]
    assert text.startswith(f"<b>{CONTRACT_TITLE}</b>\n📂 <i>Employment / NDA</i>")
    assert "🔴 Found (90% sure): non compete clause" in text and "🔴 Found (90% sure): side project ip capture" in text
    assert f"<b>non compete clause</b>: “{CONTRACT_CLAUSE}" in text  # the verified clause behind the flag
    assert "<b>Where you live (California, USA)</b>" in text and "Cal. Bus. &amp; Prof. Code § 16600" in text
    assert "Cal. Labor Code § 2870" in text and "Information, not legal advice." in text
    assert "<b>Conflicts with services you use</b>" in text
    assert "<b>Smokeco</b> (ai terms) · <i>Model Training</i> may conflict" in text
    assert ("📝 Generate Redline Kit" in [label for label, _ in buttons_of(card)]) and len(advocate.CONTRACTS) == 1
    assert len(tg.to(B_CHAT)) == b_seen  # nothing for B
    assert routes["upsert"].call_count == upserts_before  # the private check only reads from Pinecone
    assert CONTRACT_MARK not in dump(conn) and CONTRACT_CLAUSE not in dump(conn)

    # --- 3. An arbitration change with an email opt-out: draft, fill in, approve once ------------------------
    # The SSE stream starts now: it shows only events from here on.
    disconnect = _Disconnect(polls=3)
    stream = web.swarm_stream(conn, disconnect, poll=0.01)
    assert await stream.__anext__() == "retry: 5000\n\n"

    pages[TERMS_URL] = f"{TERMS}\n{DISPUTES}"
    jev.opt_out, jev.contradicts = 0.9, 0.1
    result = await agent.tick(conn, cl)
    await notify.deliver(conn, tg, cl, result.deliveries, "Fine Print: changes to terms you accepted")
    assert result.changes == 1 and result.contradictions == 0
    [alert] = [m for m in tg.to(A_CHAT) if any(d.startswith("v1:oc:") for _, d in buttons_of(m))]
    assert alert["text"].startswith("<b>Smokeco</b> changed <i>3. Disputes</i>")
    change_id = int(tapped(alert, "v1:oc:").split(":")[2])
    assert len(tg.to(B_CHAT)) == b_seen

    opt = OptOut(channel="email", target="arbitration-optout@smokeco.test", deadline_days=30,
                 required_fields=["Account username"], verbatim_instruction=INSTRUCTION)
    with extraction(opt):
        await tap(A_CHAT, alert, tapped(alert, "v1:oc:"))
    prompt = tg.to(A_CHAT)[-1]
    assert prompt["markup"]["force_reply"] is True and prompt["text"].startswith(advocate.FIELDS_HEAD)
    await say(A_CHAT, f"Account username: {NOTICE_MARK}", reply_to_text=prompt["text"])
    preview = tg.to(A_CHAT)[-1]
    assert "Email to <b>arbitration-optout@smokeco.test</b>" in preview["text"] and NOTICE_MARK in preview["text"]
    assert NOTICE_SENTENCE in preview["text"] and "Information, not legal advice." in preview["text"]
    assert conn.execute("SELECT COUNT(*) FROM approval_tokens").fetchone()[0] == 1
    approve = tapped(preview, "v1:oa:")

    await tap(A_CHAT, preview, approve)
    assert sw.kinds() == ["email"]
    _, job, hosts, detail = sw.jobs[0]
    assert job["to"] == "arbitration-optout@smokeco.test" and NOTICE_MARK in job["text"]
    assert (hosts, detail) == (["api.sprites.dev"], "agentmail_send")
    assert "Opt-out sent to Smokeco" in tg.texts(A_CHAT)[-1]
    [receipt] = conn.execute("SELECT * FROM action_receipts").fetchall()
    assert (receipt["user_id"], receipt["service_name"], receipt["action_kind"], receipt["target"], receipt["proof_ref"]) == (
        a["id"], "Smokeco", "opt_out_email", "arbitration-optout@smokeco.test", "<optout-1@agentmail.to>")
    assert set(receipt.keys()) == {"id", "user_id", "service_name", "action_kind", "target", "clause_sha", "proof_ref",
                                   "executed_at"}  # metadata only

    await tap(A_CHAT, preview, approve)  # replayed approval
    assert sw.kinds() == ["email"] and "no longer valid" in tg.texts(A_CHAT)[-1]
    assert conn.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM approval_tokens").fetchone()[0] == 0

    # --- 4. A Balanced redline kit for the contract: a tracked-changes .docx, then the contract is gone --------
    await tap(A_CHAT, card, tapped(card, "v1:rk:"))
    tones = tg.to(A_CHAT)[-1]
    assert [label for label, _ in buttons_of(tones)] == ["Polite", "Balanced", "Firm"]
    await tap(A_CHAT, tones, dict(buttons_of(tones))["Balanced"])
    assert sw.kinds() == ["email", "counterparty", "consumer", "kit"]
    [doc] = tg.documents
    assert (doc["chat"], doc["filename"], doc["mime"]) == (A_CHAT, "redline-offer-letter-and-piia.docx", advocate.DOCX_MIME)
    with zipfile.ZipFile(io.BytesIO(doc["content"])) as z:
        xml = z.read("word/document.xml").decode()
    assert "<w:ins " in xml and "<w:del " in xml and "solicit the Company's clients for six months" in xml
    summary = "\n".join(tg.texts(A_CHAT)[-2:])
    assert "Major (2.8/3) → Minor (0.8/3)" in summary and "Could we narrow the non-compete" in summary
    assert len(advocate.CONTRACTS) == 0  # the ephemeral cache is empty after delivery
    await tap(A_CHAT, tones, dict(buttons_of(tones))["Balanced"])  # again: nothing left to build from
    assert tg.texts(A_CHAT)[-1] == advocate.CONTRACT_GONE and len(sw.jobs) == 4

    # --- 5. Control Room: magic link, member-scoped Terms Radar, SSE telemetry -------------------------------
    await say(A_CHAT, "/dashboard")
    signed_in = client.get("/auth/magic", params={"token": magic_token(tg.to(A_CHAT)[-1])}, follow_redirects=False)
    assert signed_in.status_code == 303 and signed_in.headers["location"] == "/room"
    a_session = {"cookie": f"{sessions.COOKIE}={signed_in.cookies[sessions.COOKIE]}"}
    client.cookies.clear()
    page = client.get("/room", headers=a_session)
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    room = page.text
    assert "Smokeco" in room and "Otherco" not in room  # only A's service
    assert "We never use your content to train our artificial intelligence models." in room  # the contradiction
    assert "arbitration-optout@smokeco.test" in room and "opt out email" in room  # the receipt
    assert "<strong>US-CA</strong>" in room and f"/changes/{change_id}" in room
    for private in (CONTRACT_MARK, CONTRACT_CLAUSE, NOTICE_MARK, LEAKY_URL, LEAKY_EMAIL, "Bob"):
        assert private not in room
    assert client.get(f"/changes/{change_id}", headers=a_session).status_code == 200

    await say(B_CHAT, "/dashboard", name="Bob")
    b_in = client.get("/auth/magic", params={"token": magic_token(tg.to(B_CHAT)[-1])}, follow_redirects=False)
    b_session = {"cookie": f"{sessions.COOKIE}={b_in.cookies[sessions.COOKIE]}"}
    client.cookies.clear()
    assert client.get(f"/changes/{change_id}", headers=b_session).status_code == 404
    b_room = client.get("/room", headers=b_session).text
    assert "Otherco" in b_room and "Smokeco" not in b_room and "arbitration-optout" not in b_room

    frames = [frame async for frame in stream]
    events = [json.loads(f.split("data: ", 1)[1]) for f in frames if "data: " in f]
    runs = [e["detail"] for e in events if e["role"] == "advocate" and e["event"] == "run"]
    assert runs[:2] == ["agentmail_send", "sent to [redacted] via [redacted]"]
    assert {"counterparty_counsel", "consumer_counsel", "redline_build"} <= set(runs)
    assert {e["event"] for e in events} == {"egress_lock", "run", "checkpoint_restore"}
    blob = "".join(frames)
    assert LEAKY_URL not in blob and LEAKY_EMAIL not in blob and "@" not in blob and "https://" not in blob

    # --- 6. Retention: no private document text anywhere in SQLite, nor upserted to Pinecone ---------------
    stored = dump(conn)
    for private in (CONTRACT_MARK, CONTRACT_CLAUSE, QUOTES["side_project_ip_capture"], NOTICE_MARK, NOTICE_SENTENCE):
        assert private not in stored, private
    assert CONTRACT_TITLE not in stored  # not even the contract's title
    assert conn.execute("SELECT COUNT(*) FROM approval_tokens").fetchone()[0] == 0
    upserted = [c.request.content.decode() for c in routes["upsert"].calls]
    assert upserted, "public material changes are indexed"
    assert all(CONTRACT_MARK not in body and CONTRACT_CLAUSE not in body and NOTICE_MARK not in body for body in upserted)
    assert all(json.loads(body)["vectors"][0]["id"].startswith("chg-") for body in upserted)
    assert len(advocate.CONTRACTS) == len(advocate.DRAFTS) == len(advocate.PENDING_FIELDS) == 0
    await cl.aclose()

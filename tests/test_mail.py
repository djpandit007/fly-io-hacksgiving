"""Email: linking and verification, webhook authenticity, and processing of forwarded mail."""

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app import agent, db, intake, mail, users
from app.telegram import Incoming
from tests.test_bot import FakeTelegram, say

AM = "https://agentmail.test/v0/inboxes/inbox_1"
SECRET = "whsec_dGVzdC1zZWNyZXQta2V5LWZvci1zdml4"


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "mail.db")


@pytest.fixture
def tg():
    return FakeTelegram()


# --- 5.2 linking and verification ----------------------------------------------------------------

async def run(conn, tg, chat, text):
    cl = agent.Clients()
    try:
        from app import bot
        await bot.handle(conn, cl, tg, Incoming(chat_id=chat, name="M", text=text))
    finally:
        await cl.aclose()


def sent_code(route) -> str:
    body = json.loads(route.calls[-1].request.content)
    return body["text"].split("code is ")[1][:6]


@respx.mock
async def test_email_then_verify_links_the_address(conn, tg):
    send = respx.post(f"{AM}/messages/send").mock(return_value=httpx.Response(200, json={"message_id": "m1"}))
    await say(conn, tg, 2, "/start join-code")
    await run(conn, tg, 2, "/email Me@Example.com")
    assert json.loads(send.calls[0].request.content)["to"] == ["me@example.com"]
    assert users.by_email(conn, "me@example.com") is None  # not linked until verified
    await run(conn, tg, 2, f"/verify {sent_code(send)}")
    assert users.by_email(conn, "me@example.com").chat_id == 2 and tg.to(2)[-1].startswith("✅ Linked")


@respx.mock
async def test_wrong_expired_and_too_many(conn, tg):
    send = respx.post(f"{AM}/messages/send").mock(return_value=httpx.Response(200, json={}))
    await say(conn, tg, 2, "/start join-code")
    await run(conn, tg, 2, "/email me@example.com")
    await run(conn, tg, 2, "/verify 000000" if sent_code(send) != "000000" else "/verify 111111")
    assert "isn't right" in tg.to(2)[-1]

    member = users.by_chat(conn, 2)
    conn.execute("UPDATE users SET email_code_expires = ? WHERE id = ?", ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), member.id))
    await run(conn, tg, 2, f"/verify {sent_code(send)}")
    assert "expired" in tg.to(2)[-1] and users.by_email(conn, "me@example.com") is None

    await run(conn, tg, 2, "/email me@example.com")
    good = sent_code(send)
    bad = "123456" if good != "123456" else "654321"
    for _ in range(5):
        await run(conn, tg, 2, f"/verify {bad}")
    assert "Too many" in tg.to(2)[-1]
    await run(conn, tg, 2, f"/verify {good}")  # the code is void after too many attempts
    assert users.by_email(conn, "me@example.com") is None


@respx.mock
async def test_address_in_use_is_refused(conn, tg):
    respx.post(f"{AM}/messages/send").mock(return_value=httpx.Response(200, json={}))
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 3, "/start join-code")
    users.join_member(conn, "join-code", email="taken@example.com")
    await run(conn, tg, 3, "/email taken@example.com")
    assert "already linked to another member" in tg.to(3)[-1]


# --- 5.3 webhook --------------------------------------------------------------------------------

def signed(body: bytes, ts: int | None = None, msg_id: str = "msg_1") -> dict:
    ts = ts if ts is not None else int(time.time())
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": str(ts), "svix-signature": f"v1,{sig}", "content-type": "application/json"}


def event(kind="message.received", message_id="<abc@mail>"):
    return json.dumps({"event_type": kind, "event_id": "e1", "message": {
        "message_id": message_id, "inbox_id": "inbox_1", "from": "Judge <judge@example.com>", "subject": "hi"}}).encode()


def test_signature_check_unit():
    body = event()
    h = signed(body)
    assert mail.verify_signature(SECRET, h["svix-id"], h["svix-timestamp"], body, h["svix-signature"])
    assert not mail.verify_signature(SECRET, h["svix-id"], h["svix-timestamp"], body + b" ", h["svix-signature"])
    assert not mail.verify_signature(SECRET, h["svix-id"], str(int(time.time()) - 600), body, h["svix-signature"])
    assert not mail.verify_signature("", h["svix-id"], h["svix-timestamp"], body, h["svix-signature"])


def test_webhook_accepts_signed_once_and_rejects_forged_or_stale(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    processed = []

    async def fake_process(conn, cl, tg, message_id):
        processed.append(message_id)

    monkeypatch.setattr(intake, "process", fake_process)
    with TestClient(app) as client:
        body = event(message_id="<once@mail>")
        assert client.post("/agentmail", content=body, headers=signed(body)).status_code == 200
        assert client.post("/agentmail", content=body, headers=signed(body)).status_code == 200  # retry
        assert processed == ["<once@mail>"]

        forged = signed(body)
        forged["svix-signature"] = "v1,AAAA"
        assert client.post("/agentmail", content=body, headers=forged).status_code == 401
        stale_body = event(message_id="<stale@mail>")
        assert client.post("/agentmail", content=stale_body, headers=signed(stale_body, ts=int(time.time()) - 900)).status_code == 401

        other = event(kind="message.received.unauthenticated", message_id="<spoof@mail>")
        assert client.post("/agentmail", content=other, headers=signed(other)).status_code == 200
        assert processed == ["<once@mail>"]


# --- 5.4 processing ------------------------------------------------------------------------------

def message(**kw):
    base = {"message_id": "<m1@mail>", "inbox_id": "inbox_1", "from": "Member <member@example.com>",
            "subject": "Fwd: something", "text": "", "attachments": []}
    return {**base, **kw}


@pytest.fixture
def member(conn):
    m = users.join_member(conn, "join-code", chat_id=7, name="Member")
    conn.execute("UPDATE users SET email = 'member@example.com' WHERE id = ?", (m.id,))
    return users.by_id(conn, m.id)


def mock_message(**kw):
    respx.get(f"{AM}/messages/%3Cm1%40mail%3E").mock(return_value=httpx.Response(200, json=message(**kw)))
    return respx.post(f"{AM}/messages/%3Cm1%40mail%3E/reply").mock(return_value=httpx.Response(200, json={}))


async def process(conn, tg):
    cl = agent.Clients()
    try:
        await intake.process(conn, cl, tg, "<m1@mail>")
    finally:
        await cl.aclose()


@respx.mock
async def test_unknown_sender_is_ignored_silently(conn, tg):
    reply = mock_message(**{"from": "stranger@example.com", "text": "x" * 500})
    await process(conn, tg)
    assert not reply.called and tg.sent == []


@respx.mock
async def test_join_code_in_subject_creates_email_only_member(conn, tg, monkeypatch):
    reply = mock_message(**{"from": "Judge <judge@example.com>", "subject": "Fine Print join-code please",
                            "text": "Terms text. " * 60})

    async def fake_card(cl, title, text, domain=None):
        return "<b>Card</b>"

    monkeypatch.setattr(agent, "verdict_card", fake_card)
    await process(conn, tg)
    judge = users.by_email(conn, "judge@example.com")
    assert judge and judge.chat_id is None and judge.name == "Judge"
    assert "**Card**" in json.loads(reply.calls[0].request.content)["text"]


@respx.mock
async def test_attachment_goes_to_intake_and_replies_on_both_channels(conn, tg, member, monkeypatch):
    reply = mock_message(attachments=[{"attachment_id": "a1", "filename": "lease.pdf", "content_type": "application/pdf", "size": 10}])
    respx.get(f"{AM}/messages/%3Cm1%40mail%3E/attachments/a1").mock(
        return_value=httpx.Response(200, json={"download_url": "https://files.agentmail.test/a1"}))
    respx.get("https://files.agentmail.test/a1").mock(return_value=httpx.Response(200, content=b"%PDF-1.4"))
    seen = []

    async def fake_intake(conn_, cl, user, filename, content):
        seen.append((user.id, filename, content))
        return "<b>Lease verdict</b>"

    monkeypatch.setattr(agent, "intake_file", fake_intake)
    await process(conn, tg)
    assert seen == [(member.id, "lease.pdf", b"%PDF-1.4")]
    assert tg.to(7) == ["<b>Lease verdict</b>"]
    assert "**Lease verdict**" in json.loads(reply.calls[0].request.content)["text"]


@respx.mock
async def test_terms_update_subscribes_checks_and_fans_out(conn, tg, member, monkeypatch):
    other = users.join_member(conn, "join-code", chat_id=8, name="Other")
    text = "---------- Forwarded message ---------\nFrom: Spotify <no-reply@spotify.com>\nWe've updated our Terms of Use."
    reply = mock_message(subject="Fwd: We've updated our Terms of Use", text=text)
    asked = []

    async def fake_find(query, cl=None, client=None):
        asked.append(query)
        return "https://www.spotify.com/us/legal/end-user-agreement"

    svc = agent.add_service(conn, "Spotify", "https://www.spotify.com/us/legal/end-user-agreement")
    conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (other.id, svc.id, db.now()))
    change = db.Change(id=1, service_id=svc.id, from_snap=None, to_snap=1, detected_at=datetime.now(UTC),
                       effective_at=datetime.now(UTC), heading="Arbitration", kind="added", before_text="",
                       after_text="Binding arbitration.", material=0.9, category="arbitration", opt_out=0.1,
                       severity=2.5, summary="New arbitration clause.", favours="company")

    async def fake_subscribe(conn_, cl, user, name, url):
        conn_.execute("INSERT OR IGNORE INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (user.id, svc.id, db.now()))
        return svc, []

    async def fake_check(conn_, cl, s):
        return [change]

    monkeypatch.setattr(intake, "find_terms_url", fake_find)
    monkeypatch.setattr(agent, "subscribe", fake_subscribe)
    monkeypatch.setattr(agent, "check_service", fake_check)
    await process(conn, tg)
    assert asked == ["spotify.com"]
    assert any("New arbitration clause" in t for t in tg.to(7))   # the sender
    assert any("New arbitration clause" in t for t in tg.to(8))   # the other watcher, right away
    assert reply.called


@respx.mock
async def test_terms_link_and_long_body_and_short_body(conn, tg, member, monkeypatch):
    calls = []

    async def fake_url(cl, url):
        calls.append(("url", url))
        return "<b>URL card</b>"

    async def fake_card(cl, title, text, domain=None):
        calls.append(("text", title))
        return "<b>Text card</b>"

    monkeypatch.setattr(agent, "check_url", fake_url)
    monkeypatch.setattr(agent, "verdict_card", fake_card)

    mock_message(text="Please read https://acme.test/legal/terms before signing.")
    await process(conn, tg)
    respx.reset()
    mock_message(subject="Fwd: My gym contract", text="Clause. " * 80)
    await process(conn, tg)
    respx.reset()
    reply = mock_message(text="hello")
    await process(conn, tg)

    assert calls == [("url", "https://acme.test/legal/terms"), ("text", "My gym contract")]
    assert "didn't find anything to check" in json.loads(reply.calls[0].request.content)["text"]


@respx.mock
async def test_email_contents_are_not_stored(conn, tg, member, monkeypatch):
    marker = "UNIQUE-BODY-MARKER-12345"
    mock_message(subject="Fwd: contract", text=(marker + " ") * 50)

    async def fake_card(cl, title, text, domain=None):
        return "<b>ok</b>"

    monkeypatch.setattr(agent, "verdict_card", fake_card)
    await process(conn, tg)
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for row in conn.execute(f"SELECT * FROM {table}").fetchall():
            assert marker not in " ".join(str(v) for v in tuple(row)), table


@respx.mock
async def test_deliver_uses_telegram_for_chats_and_one_digest_for_email_only(conn, tg):
    from app import notify

    send = respx.post(f"{AM}/messages/send").mock(return_value=httpx.Response(200, json={}))
    chat_member = users.join_member(conn, "join-code", chat_id=7)
    email_member = users.join_member(conn, "join-code", email="judge@example.com")
    cl = agent.Clients()
    sent = await notify.deliver(conn, tg, cl, {chat_member.id: ["<b>one</b>", "two"], email_member.id: ["<b>a</b>", "b"]}, "Subject")
    await cl.aclose()
    assert tg.to(7) == ["<b>one</b>", "two"] and send.call_count == 1 and sent == 3
    body = json.loads(send.calls[0].request.content)
    assert body["to"] == ["judge@example.com"] and "**a**" in body["text"] and "b" in body["text"]

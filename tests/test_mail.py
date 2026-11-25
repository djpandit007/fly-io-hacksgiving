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
    send = respx.post(f"{AM}/messages/send", name="send").mock(return_value=httpx.Response(200, json={"message_id": "m1"}))
    await say(conn, tg, 2, "/start join-code")
    await run(conn, tg, 2, "/email Me@Example.com")
    assert json.loads(send.calls[0].request.content)["to"] == ["me@example.com"]
    assert users.by_email(conn, "me@example.com") is None  # not linked until verified
    await run(conn, tg, 2, f"/verify {sent_code(send)}")
    assert users.by_email(conn, "me@example.com").chat_id == 2 and tg.to(2)[-1].startswith("✅ Linked")


@respx.mock
async def test_wrong_expired_and_too_many(conn, tg):
    send = respx.post(f"{AM}/messages/send", name="send").mock(return_value=httpx.Response(200, json={}))
    await say(conn, tg, 2, "/start join-code")
    await run(conn, tg, 2, "/email me@example.com")
    await run(conn, tg, 2, "/verify 000000" if sent_code(send) != "000000" else "/verify 111111")
    assert "isn't right" in tg.to(2)[-1]

    member = users.by_chat(conn, 2)
    conn.execute("UPDATE users SET email_code_expires = ? WHERE id = ?", ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), member.id))
    await run(conn, tg, 2, f"/verify {sent_code(send)}")
    assert "expired" in tg.to(2)[-1] and users.by_email(conn, "me@example.com") is None

    conn.execute("UPDATE email_codes SET sent_at = '2000-01-01T00:00:00+00:00'")  # past the resend cooldown
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
    respx.post(f"{AM}/messages/send", name="send").mock(return_value=httpx.Response(200, json={}))
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

    async def fake_process(conn, cl, tg, message_id, thread_id=None):
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
    base = {"message_id": "<m1+x==@mail>", "thread_id": "t1", "inbox_id": "inbox_1",
            "from": "Member <member@example.com>", "subject": "Fwd: something", "text": "", "attachments": []}
    msg = {**base, **kw}
    if "authentication_results" not in kw:
        domain = msg["from"].rsplit("@", 1)[1].rstrip(">").strip()
        msg["authentication_results"] = {"spf": "pass", "dkim": "pass", "dmarc": "pass", "dmarc_domain": domain}
    return msg


@pytest.fixture
def member(conn):
    m = users.join_member(conn, "join-code", chat_id=7, name="Member")
    conn.execute("UPDATE users SET email = 'member@example.com' WHERE id = ?", (m.id,))
    return users.by_id(conn, m.id)


def outbox():
    """The shared send route: replies are sent as new "Re:" emails."""
    try:
        return respx.routes["send"]
    except KeyError:
        return respx.post(f"{AM}/messages/send", name="send").mock(return_value=httpx.Response(200, json={}))


def mock_message(**kw):
    """Serve one received message through its thread (message endpoints are refused by the connector)."""
    respx.get(f"{AM}/threads/t1").mock(return_value=httpx.Response(200, json={"thread_id": "t1", "messages": [message(**kw)]}))
    return outbox()


async def process(conn, tg):
    cl = agent.Clients()
    try:
        await intake.process(conn, cl, tg, "<m1+x==@mail>", "t1")
    finally:
        await cl.aclose()


@respx.mock
async def test_unknown_sender_is_ignored_silently(conn, tg):
    reply = mock_message(**{"from": "stranger@example.com", "text": "x" * 500})
    await process(conn, tg)
    assert not reply.called and tg.sent == []


@respx.mock
async def test_join_code_email_needs_a_reply_proving_the_mailbox(conn, tg, monkeypatch):
    send = respx.post(f"{AM}/messages/send", name="send").mock(return_value=httpx.Response(200, json={}))
    reply = mock_message(**{"from": "Judge <judge@example.com>", "subject": "Fine Print join-code please",
                            "text": "Terms text. " * 60})
    checked = []

    async def fake_card(cl, title, text, domain=None):
        checked.append(title)
        return "<b>Card</b>"

    monkeypatch.setattr(agent, "verdict_card", fake_card)
    await process(conn, tg)
    # Not a member yet, address not verified, nothing processed: only a confirmation went to the address.
    assert users.by_email(conn, "judge@example.com") is None and checked == []
    [only] = [json.loads(c.request.content) for c in reply.calls]
    assert only["to"] == ["judge@example.com"] and only["subject"] == "Confirm your email for Fine Print"
    pending = users.pending_by_address(conn, "judge@example.com")
    assert pending and pending.status == "pending" and pending not in users.members(conn)
    import re
    code = re.search(r"\b(\d{6})\b", json.loads(send.calls[0].request.content)["text"]).group(1)

    respx.reset()
    send = respx.post(f"{AM}/messages/send", name="send").mock(return_value=httpx.Response(200, json={}))
    confirm = mock_message(**{"from": "judge@example.com", "subject": "Re: Confirm your email for Fine Print",
                              "text": f"Yes it's me. {code}"})
    await process(conn, tg)
    judge = users.by_email(conn, "judge@example.com")
    assert judge and judge.status == "active" and judge.chat_id is None and judge.name == "Judge"
    assert "Confirmed" in json.loads(confirm.calls[0].request.content)["text"] and checked == []

    respx.reset()
    reply = mock_message(**{"from": "judge@example.com", "subject": "Fwd: contract", "text": "Terms text. " * 60})
    await process(conn, tg)
    assert checked == ["contract"] and "**Card**" in json.loads(reply.calls[0].request.content)["text"]


@respx.mock
async def test_spoofed_join_does_not_take_over_a_strangers_address(conn, tg):
    respx.post(f"{AM}/messages/send", name="send").mock(return_value=httpx.Response(200, json={}))
    mock_message(**{"from": "victim@example.com", "subject": "join-code", "text": "x"})
    await process(conn, tg)
    # The victim's address is not verified for anyone, so they can still link it themselves.
    assert users.by_email(conn, "victim@example.com") is None
    await say(conn, tg, 5, "/start join-code")
    await run(conn, tg, 5, "/email victim@example.com")
    assert "already linked" not in tg.to(5)[-1]


@respx.mock
async def test_mail_flagged_by_agentmail_is_ignored(conn, tg, member):
    reply = mock_message(labels=["unauthenticated"], text="x" * 500)
    await process(conn, tg)
    assert not reply.called and tg.sent == []


@respx.mock
async def test_email_code_rate_limits_and_lockout(conn, tg):
    send = respx.post(f"{AM}/messages/send", name="send").mock(return_value=httpx.Response(200, json={}))
    await say(conn, tg, 2, "/start join-code")
    await run(conn, tg, 2, "/email a@example.com")
    await run(conn, tg, 2, "/email a@example.com")
    assert "wait a minute" in tg.to(2)[-1] and send.call_count == 1

    member = users.by_chat(conn, 2)
    for _ in range(2):  # per-address daily cap (3)
        conn.execute("UPDATE email_codes SET sent_at = ?", ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(),))
        await run(conn, tg, 2, "/email a@example.com")
    conn.execute("UPDATE email_codes SET sent_at = ?", ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(),))
    await run(conn, tg, 2, "/email a@example.com")
    assert "enough codes" in tg.to(2)[-1] and send.call_count == 3

    # Cumulative failures across codes lock linking for a day; /email does not reset them.
    conn.execute("UPDATE users SET email_failures = 9 WHERE id = ?", (member.id,))
    await run(conn, tg, 2, "/verify 999999" if sent_code(send) != "999999" else "/verify 888888")
    assert "paused for 24 hours" in tg.to(2)[-1]
    conn.execute("DELETE FROM email_codes")
    await run(conn, tg, 2, "/email b@example.com")
    assert "try again tomorrow" in tg.to(2)[-1]


def test_forwarded_from_parsing_is_bounded_and_ignores_long_lines():
    long_line = "From: " + "a" * 100_000 + "@evil.test"
    text = long_line + "\n> From: Spotify <no-reply@spotify.com>\n"
    started = time.perf_counter()
    msg = mail.Message(message_id="m", **{"from": "member@example.com"})
    assert intake.original_sender_domain(msg, text) == "spotify.com"
    assert time.perf_counter() - started < 0.5


@respx.mock
async def test_attachment_goes_to_intake_and_replies_on_both_channels(conn, tg, member, monkeypatch):
    reply = mock_message(attachments=[{"attachment_id": "a1", "filename": "lease.pdf", "content_type": "application/pdf", "size": 10}])
    respx.get(f"{AM}/threads/t1/attachments/a1").mock(
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

    send = respx.post(f"{AM}/messages/send", name="send").mock(return_value=httpx.Response(200, json={}))
    chat_member = users.join_member(conn, "join-code", chat_id=7)
    email_member = users.join_member(conn, "join-code", email="judge@example.com")
    cl = agent.Clients()
    sent = await notify.deliver(conn, tg, cl, {chat_member.id: ["<b>one</b>", "two"], email_member.id: ["<b>a</b>", "b"]}, "Subject")
    await cl.aclose()
    assert tg.to(7) == ["<b>one</b>", "two"] and send.call_count == 1 and sent == 3
    body = json.loads(send.calls[0].request.content)
    assert body["to"] == ["judge@example.com"] and "**a**" in body["text"] and "b" in body["text"]


@respx.mock
async def test_locked_pending_address_stays_locked_across_new_join_emails(conn, tg):
    send = respx.post(f"{AM}/messages/send", name="send").mock(return_value=httpx.Response(200, json={}))
    mock_message(**{"from": "target@example.com", "subject": "join-code", "text": "x"})
    await process(conn, tg)
    pending = users.pending_by_address(conn, "target@example.com")
    conn.execute("UPDATE users SET email_failures = 9 WHERE id = ?", (pending.id,))
    # Make the next guess certainly wrong, whatever code was issued.
    conn.execute("UPDATE users SET email_code_hash = 'not-a-real-hash' WHERE id = ?", (pending.id,))
    assert users.confirm_email_only(conn, "target@example.com", "000000") == "locked"
    locked = users.by_id(conn, pending.id)
    assert locked.email_locked_until is not None

    respx.reset()
    send = respx.post(f"{AM}/messages/send", name="send").mock(return_value=httpx.Response(200, json={}))
    conn.execute("DELETE FROM email_codes")  # even with the daily caps out of the way
    mock_message(**{"from": "target@example.com", "subject": "join-code again", "text": "x"})
    await process(conn, tg)
    assert not send.called  # no new code for a locked address
    rows = conn.execute("SELECT COUNT(*) FROM users WHERE email_pending = 'target@example.com'").fetchone()[0]
    assert rows == 1  # the same pending row, not a fresh one


def test_nested_forward_picks_the_company_named_in_the_subject():
    text = ("Regards,\n\n---------- Forwarded message ---------\nFrom: Dhananjay <dvpandit@asu.edu>\n"
            "Subject: Fwd: Updates to Discord's Policies\n\n---------- Forwarded message ---------\n"
            "From: Discord <notifications@discord.com>\nWe're updating our Terms.")
    msg = mail.Message(message_id="m", subject="Fwd: Updates to Discord’s Policies", **{"from": "me@gmail.com"})
    assert intake.original_sender_domain(msg, text) == "discord.com"
    # Without a name in the subject, the deepest forwarded sender (the original) wins.
    msg2 = mail.Message(message_id="m", subject="Fwd: Fwd: policy update", **{"from": "me@gmail.com"})
    assert intake.original_sender_domain(msg2, text) == "discord.com"


@pytest.mark.parametrize("auth", [
    None,                                                                     # no results: fail closed
    {"spf": "fail", "dkim": "fail", "dmarc": "fail", "dmarc_domain": "example.com"},
    {"spf": "pass", "dkim": "pass", "dmarc": "pass", "dmarc_domain": "attacker.test",
     "dkim_domain": "attacker.test", "spf_domain": "attacker.test"},          # passes, but not for the From domain
])
@respx.mock
async def test_unauthenticated_or_misaligned_mail_is_ignored(conn, tg, member, auth):
    send = mock_message(authentication_results=auth, text="x" * 500)
    await process(conn, tg)
    assert not send.called and tg.sent == []


@respx.mock
async def test_unreachable_attachment_points_to_telegram(conn, tg, member):
    send = mock_message(attachments=[{"attachment_id": "a1", "filename": "lease.pdf", "size": 10}])
    respx.get(f"{AM}/threads/t1/attachments/a1").mock(return_value=httpx.Response(403, json={"error": "not supported"}))
    respx.get(url__regex=rf"{AM}/messages/.*/attachments/a1").mock(return_value=httpx.Response(403, json={}))
    await process(conn, tg)
    assert "Send it to the Fine Print bot in Telegram" in tg.to(7)[0]
    assert json.loads(send.calls[0].request.content)["subject"].startswith("Re: ")


@pytest.mark.parametrize("subject", [
    "Fwd: Updates to Discord’s Policies", "We've updated our Terms of Service", "Changes to our User Agreement",
    "Fwd: Fwd: Revised Conditions of Use", "Our new privacy policy",
])
def test_update_subject_matches_plurals_and_word_forms(subject):
    assert intake.UPDATE_SUBJECT.search(subject)


def test_update_subject_ignores_ordinary_mail():
    assert not intake.UPDATE_SUBJECT.search("Your order has shipped")

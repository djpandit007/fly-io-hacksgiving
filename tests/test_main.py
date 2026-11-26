from fastapi.testclient import TestClient

from app.main import app


def test_routes_are_guarded():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/tick").status_code == 401
        assert client.post("/tick", headers={"X-Fine-Print-Secret": "wrong"}).status_code == 401
        assert client.post("/telegram", json={}).status_code == 401
        assert client.get("/calendar.ics").status_code == 401


def test_calendar_is_per_member():
    from datetime import date, timedelta

    from app import db, ledger, users
    from app.config import settings

    with TestClient(app) as client:
        conn = db.connect(settings.db_path)  # the app's connection lives on the server thread
        a = users.join_member(conn, "join-code", chat_id=1001, name="A")
        b = users.join_member(conn, "join-code", chat_id=1002, name="B")
        ledger.add(conn, a.id, "A only", date.today() + timedelta(days=10), "notice")
        ledger.add(conn, b.id, "B only", date.today() + timedelta(days=10), "notice")
        resp = client.get("/calendar.ics", params={"token": a.calendar_token})
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/calendar")
        assert "A only" in resp.text and "B only" not in resp.text
        assert client.get("/calendar.ics", params={"token": "cal-token"}).status_code in (200, 401)  # legacy owner token
        assert client.get("/calendar.ics", params={"token": "nope"}).status_code == 401


def test_telegram_from_stranger_is_ignored():
    with TestClient(app) as client:
        resp = client.post(
            "/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
            json={"update_id": 1, "message": {"message_id": 1, "chat": {"id": 99}, "text": "hello"}},
        )
        assert resp.json() == {"ok": True}  # accepted, but no owner yet and not /start: nothing sent


async def test_quick_tap_is_not_queued_behind_a_long_job_in_the_same_chat(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from app import db, main, users
    from app.locks import locks
    from tests.test_bot import FakeTelegram

    conn = db.connect(tmp_path / "m.db")
    users.join_member(conn, "join-code", chat_id=2)
    tg = FakeTelegram()
    app_ = SimpleNamespace(state=SimpleNamespace(conn=conn, clients=None, tg=tg))
    tap = {"update_id": 5, "callback_query": {"id": "cb", "from": {"id": 2}, "data": "v1:n",
                                              "message": {"message_id": 7, "chat": {"id": 2}, "date": 0}}}
    async with locks.hold("chat:2"):  # a long check is running in this chat
        await asyncio.wait_for(main._handle_message(app_, tap, "telegram-5"), timeout=1)
    assert tg.called("answer") == [("answer", "cb", "OK", False)]


async def test_slow_tap_is_answered_before_it_queues(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from app import db, main, users
    from app.locks import locks
    from tests.test_bot import FakeTelegram

    conn = db.connect(tmp_path / "m.db")
    users.join_member(conn, "join-code", chat_id=2)
    tg = FakeTelegram()
    app_ = SimpleNamespace(state=SimpleNamespace(conn=conn, clients=None, tg=tg))
    tap = {"update_id": 6, "callback_query": {"id": "cb", "from": {"id": 2}, "data": "v1:w:notion",
                                              "message": {"message_id": 7, "chat": {"id": 2}, "date": 0}}}
    async with locks.hold("chat:2"):
        job = asyncio.create_task(main._handle_message(app_, tap, "telegram-6"))
        await asyncio.sleep(0.05)
        assert tg.called("answer") == [("answer", "cb", "On it…", False)] and not job.done()
    job.cancel()


def _slow_tap(update_id, chat=2, message_id=7):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "from": {"id": chat}, "data": "v1:w:notion",
        "message": {"message_id": message_id, "chat": {"id": chat}, "date": 1, "reply_markup": {"inline_keyboard": [
            [{"text": "Yes, watch it", "callback_data": "v1:w:notion"}, {"text": "No", "callback_data": "v1:n"}]]}}}}


def _app(tmp_path, *chats):
    from types import SimpleNamespace

    from app import db, users
    from tests.test_bot import FakeTelegram

    conn = db.connect(tmp_path / "m.db")
    for chat in chats:
        users.join_member(conn, "join-code", chat_id=chat)
    return SimpleNamespace(state=SimpleNamespace(conn=conn, clients=None, tg=FakeTelegram())), conn


async def test_slow_tap_from_a_removed_member_gets_the_private_notice(tmp_path):
    import asyncio

    from app import bot, main, users
    from app.locks import locks

    app_, conn = _app(tmp_path, 2)
    users.remove(conn, users.by_chat(conn, 2).id)
    async with locks.hold("chat:2"):  # not queued behind the chat's work either
        await asyncio.wait_for(main._handle_message(app_, _slow_tap(10), "telegram-10"), timeout=1)
    tg = app_.state.tg
    assert tg.called("answer") == [("answer", "cb10", bot.PRIVATE_TAP, True)] and not tg.called("edit")


async def test_slow_tap_drops_its_buttons_before_queueing_and_runs_once(tmp_path, monkeypatch):
    import asyncio

    from app import bot, main
    from app.locks import locks

    watched = []

    async def fake_watch(conn_, cl, tg_, user, arg, reply_to):
        watched.append(arg)

    monkeypatch.setattr(bot, "_watch", fake_watch)
    app_, _ = _app(tmp_path, 2)
    tg = app_.state.tg
    async with locks.hold("chat:2"):
        first = asyncio.create_task(main._handle_message(app_, _slow_tap(11), "telegram-11"))
        await asyncio.sleep(0.05)
        assert tg.called("edit") == [("edit", 2, 7, None)]  # button gone while still queued
        await main._handle_message(app_, _slow_tap(12), "telegram-12")  # a double tap
    await first
    assert watched == ["notion"]
    assert [c[2] for c in tg.called("answer")] == ["On it…", "Already on it."]
    assert len(tg.called("edit")) == 1  # the bot doesn't try to remove them again

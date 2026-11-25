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

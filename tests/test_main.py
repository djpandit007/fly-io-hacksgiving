from fastapi.testclient import TestClient

from app.main import app


def test_routes_are_guarded():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/tick").status_code == 401
        assert client.post("/tick", headers={"X-Fine-Print-Secret": "wrong"}).status_code == 401
        assert client.post("/telegram", json={}).status_code == 401
        assert client.get("/calendar.ics").status_code == 401


def test_calendar_with_token():
    with TestClient(app) as client:
        resp = client.get("/calendar.ics", params={"token": "cal-token"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/calendar")
        assert "BEGIN:VCALENDAR" in resp.text


def test_telegram_from_stranger_is_ignored():
    with TestClient(app) as client:
        resp = client.post(
            "/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
            json={"update_id": 1, "message": {"message_id": 1, "chat": {"id": 99}, "text": "hello"}},
        )
        assert resp.json() == {"ok": True}  # accepted, but no owner yet and not /start: nothing sent

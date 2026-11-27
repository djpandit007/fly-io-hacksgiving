"""Web: the public Terms Radar and change inspector leak no member data, Control Room sign-in is
single-use and member-scoped, revocation is immediate, and the swarm stream is sanitized."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import agent, db, ledger, sessions, users, web
from app.config import settings
from app.main import app

# Seeded member data that must never appear on a public page.
SECRETS = ("Private Person", "leaky-", "Secret deadline title", "optout@corp.test", "proofref-secret", "US-CA")


@pytest.fixture
def client():
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def conn():
    # The app's own connection lives on the server thread; this one shares the same database file.
    return db.connect(settings.db_path)


def _member(conn, chat_id, name, email=None):
    user = users.join_member(conn, "join-code", chat_id=chat_id, name=name)
    if email:
        conn.execute("UPDATE users SET email = ?, jurisdiction = 'US-CA' WHERE id = ?", (email, user.id))
    return users.by_id(conn, user.id)


def _seed(conn, suffix: str, chat_id: int):
    """A public service with a material change and a contradiction, plus one member's private data."""
    svc = agent.add_service(conn, f"Radar{suffix}", f"https://radar{suffix}.test/terms")
    conn.execute(  # add_service may or may not record the terms document itself
        "INSERT OR IGNORE INTO service_documents (service_id, role, url, added_at) VALUES (?, 'terms', ?, ?)",
        (svc.id, svc.terms_url, db.now()),
    )
    terms = conn.execute("SELECT id FROM service_documents WHERE service_id = ? AND role = 'terms'", (svc.id,)).fetchone()[0]
    privacy = conn.execute(
        "INSERT INTO service_documents (service_id, role, url, added_at) VALUES (?, 'privacy', ?, ?)",
        (svc.id, f"https://radar{suffix}.test/privacy", db.now()),
    ).lastrowid
    s1 = conn.execute(
        "INSERT INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (?, ?, 'live', ?, 'old')",
        (svc.id, db.now(), f"a{suffix}"),
    ).lastrowid
    s2 = conn.execute(
        "INSERT INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (?, ?, 'live', ?, 'new')",
        (svc.id, db.now(), f"b{suffix}"),
    ).lastrowid
    change_id = conn.execute(
        """INSERT INTO changes (service_id, from_snap, to_snap, detected_at, effective_at, heading, kind, before_text,
               after_text, material, category, opt_out, severity, favours, summary, document_id)
           VALUES (?, ?, ?, ?, ?, 'Arbitration', 'modified', ?, ?, 0.93, 'dispute_resolution', 0.2, 0.81, 'company',
                   'Disputes now go to <b>arbitration</b>.', ?)""",
        (svc.id, s1, s2, db.now(), db.now(), "You may sue us in court.", "You must arbitrate <script>alert(1)</script>.", terms),
    ).lastrowid
    conn.execute(
        """INSERT INTO document_contradictions (service_id, doc_a_id, doc_b_id, heading_a, heading_b, quote_a, quote_b,
               summary, severity, detected_at) VALUES (?, ?, ?, 'Data', 'Sharing', 'We never sell.', 'We may sell.',
               'Terms and Privacy disagree on selling data.', 0.75, ?)""",
        (svc.id, terms, privacy, db.now()),
    )
    member = _member(conn, chat_id, "Private Person", f"leaky-{suffix}@example.com")
    conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (member.id, svc.id, db.now()))
    ledger.add(conn, member.id, "Secret deadline title", date.today() + timedelta(days=5), "opt_out", service_id=svc.id)
    conn.execute(
        """INSERT INTO action_receipts (user_id, service_name, action_kind, target, clause_sha, proof_ref, executed_at)
           VALUES (?, ?, 'opt_out_email', 'optout@corp.test', 'sha', 'proofref-secret', ?)""",
        (member.id, svc.name, db.now()),
    )
    return svc, change_id, s2, member


def _assert_no_member_data(text: str, member) -> None:
    for secret in (*SECRETS, member.email or "", member.calendar_token, str(member.chat_id)):
        assert secret and secret not in text, secret


def test_public_radar_shows_public_data_and_no_member_data(client, conn):
    svc, change_id, _, member = _seed(conn, "pub", 7001)
    resp = client.get("/")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")
    page = resp.text
    assert svc.name in page and f"/changes/{change_id}" in page and "Arbitration" in page
    assert "Terms and Privacy disagree on selling data." in page and "Privacy Policy" in page
    assert "<b>arbitration</b>" not in page  # summaries are escaped
    _assert_no_member_data(page, member)


def test_public_change_inspector(client, conn):
    _, change_id, _, member = _seed(conn, "chg", 7002)
    resp = client.get(f"/changes/{change_id}")
    assert resp.status_code == 200
    page = resp.text
    assert "<del>" in page and "<ins>" in page and "court." in page
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert 'aria-valuenow="93"' in page and 'aria-valuenow="81"' in page and "company" in page
    assert "dispute resolution" in page and "Benchmark" not in page  # no benchmark hook installed
    _assert_no_member_data(page, member)


def test_unknown_change_is_404(client):
    assert client.get("/changes/999999").status_code == 404
    assert client.get("/changes/999999/evidence.png").status_code == 404


def test_benchmark_hook_shows_a_badge_and_failures_hide_it(client, conn, monkeypatch):
    _, change_id, _, _ = _seed(conn, "bench", 7003)

    async def hook(request, change):
        return SimpleNamespace(percentile=88.4, cohort_size=14, opt_out_share=0.25)

    monkeypatch.setattr(web, "benchmark_hook", hook)
    page = client.get(f"/changes/{change_id}").text
    assert "more severe than 88% of comparable public clauses (14 services)" in page and "25%" in page

    async def broken(request, change):
        raise RuntimeError("pinecone down")

    monkeypatch.setattr(web, "benchmark_hook", broken)
    resp = client.get(f"/changes/{change_id}")
    assert resp.status_code == 200 and "Benchmark" not in resp.text


def test_evidence_is_served_only_from_the_evidence_dir(client, conn):
    _, change_id, snap, _ = _seed(conn, "ev", 7004)
    shot = settings.evidence_dir / "1" / "terms" / "abc.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    conn.execute("UPDATE snapshots SET evidence_path = ? WHERE id = ?", ("1/terms/abc.png", snap))
    assert "/evidence.png" in client.get(f"/changes/{change_id}").text
    resp = client.get(f"/changes/{change_id}/evidence.png")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png" and resp.content.startswith(b"\x89PNG")

    outside = settings.data_dir / "outside.png"
    outside.write_bytes(b"\x89PNG secret")
    for path in ("../outside.png", str(outside), "../fineprint.db"):
        conn.execute("UPDATE snapshots SET evidence_path = ? WHERE id = ?", (path, snap))
        assert client.get(f"/changes/{change_id}/evidence.png").status_code == 404
        assert "/evidence.png" not in client.get(f"/changes/{change_id}").text


# --- Control Room ----------------------------------------------------------------------------------

@pytest.fixture
def linked(monkeypatch):
    monkeypatch.setattr(sessions, "settings", settings.model_copy(update={"public_url": "https://fp.test"}))


def _token(link: str) -> str:
    assert link.startswith("https://fp.test/auth/magic?token=")
    return link.split("token=", 1)[1]


def _login(client, conn, user) -> str:
    resp = client.get("/auth/magic", params={"token": _token(sessions.magic_link(conn, user))}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/room"
    cookie = resp.headers["set-cookie"]
    assert all(flag in cookie.lower() for flag in ("httponly", "secure", "samesite=lax", "max-age=86400"))
    return resp.cookies[sessions.COOKIE]


def test_magic_link_signs_in_once_and_shows_only_that_members_room(client, conn, linked):
    _, _, _, a = _seed(conn, "roomA", 7005)
    b = _member(conn, 8801, "Other Member")
    other = agent.add_service(conn, "OtherOnly", "https://otheronly.test/terms")
    conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (b.id, other.id, db.now()))
    ledger.add(conn, b.id, "B private deadline", date.today() + timedelta(days=3), "notice")

    link = sessions.magic_link(conn, a)
    stored = conn.execute("SELECT token_hash FROM dashboard_sessions WHERE user_id = ? AND kind = 'magic'", (a.id,)).fetchone()[0]
    assert _token(link) not in stored and len(stored) == 64  # only the SHA-256 is stored

    resp = client.get("/auth/magic", params={"token": _token(link)})  # follows the redirect
    assert resp.status_code == 200 and resp.url.path == "/room"
    page = resp.text
    assert "Radarroom" in page and "Secret deadline title" in page and "optout@corp.test" in page and "US-CA" in page
    assert f"/calendar.ics?token={a.calendar_token}" in page and "Privacy Policy" in page
    assert "OtherOnly" not in page and "B private deadline" not in page and "Other Member" not in page
    assert resp.headers["cache-control"] == "no-store"

    # Replay: the same link no longer works, and says how to get a new one.
    client.cookies.clear()
    replay = client.get("/auth/magic", params={"token": _token(link)})
    assert replay.status_code == 401 and "/dashboard" in replay.text and "Radarroom" not in replay.text


def test_expired_or_bogus_magic_links_are_denied(client, conn, linked):
    user = _member(conn, 8802, "Late")
    old = sessions.magic_link(conn, user, now=datetime.now(UTC) - timedelta(minutes=11))
    resp = client.get("/auth/magic", params={"token": _token(old)})
    assert resp.status_code == 401 and "/dashboard" in resp.text
    assert client.get("/auth/magic", params={"token": "nope"}).status_code == 401
    assert client.get("/auth/magic").status_code == 401


def test_a_newer_link_replaces_the_older_one(client, conn, linked):
    user = _member(conn, 8803, "Twice")
    first = sessions.magic_link(conn, user)
    sessions.magic_link(conn, user)
    assert client.get("/auth/magic", params={"token": _token(first)}).status_code == 401


def test_room_needs_a_genuine_current_session(client, conn, linked):
    user = _member(conn, 8804, "Forger")
    assert client.get("/room").status_code == 401
    cookie = _login(client, conn, user)
    client.cookies.clear()
    raw, _, sig = cookie.rpartition(".")
    for bad in (f"{raw}.{'0' * 64}", raw, f"x.{sig}", ""):
        assert client.get("/room", headers={"cookie": f"{sessions.COOKIE}={bad}"}).status_code == 401
    assert client.get("/room", headers={"cookie": f"{sessions.COOKIE}={cookie}"}).status_code == 200
    # An expired session is refused.
    conn.execute("UPDATE dashboard_sessions SET expires_at = ? WHERE user_id = ? AND kind = 'session'",
                 ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds"), user.id))
    assert client.get("/room", headers={"cookie": f"{sessions.COOKIE}={cookie}"}).status_code == 401


def test_remove_revokes_control_room_sessions_immediately(client, conn, linked):
    user = _member(conn, 8805, "Removed Soon")
    cookie = _login(client, conn, user)
    pending = sessions.magic_link(conn, user)
    client.cookies.clear()
    assert client.get("/room", headers={"cookie": f"{sessions.COOKIE}={cookie}"}).status_code == 200
    assert users.remove(conn, user.id)
    assert conn.execute("SELECT COUNT(*) FROM dashboard_sessions WHERE user_id = ?", (user.id,)).fetchone()[0] == 0
    assert client.get("/room", headers={"cookie": f"{sessions.COOKIE}={cookie}"}).status_code == 401
    assert client.get("/auth/magic", params={"token": _token(pending)}).status_code == 401


def test_inactive_member_cannot_exchange_a_link(client, conn, linked):
    user = _member(conn, 8806, "Inactive")
    link = sessions.magic_link(conn, user)
    conn.execute("UPDATE users SET status = 'removed' WHERE id = ?", (user.id,))  # not via remove(): rows remain
    assert client.get("/auth/magic", params={"token": _token(link)}).status_code == 401


def test_logout_ends_the_session(client, conn, linked):
    user = _member(conn, 8807, "Leaving")
    cookie = _login(client, conn, user)
    client.cookies.clear()
    headers = {"cookie": f"{sessions.COOKIE}={cookie}"}
    assert client.post("/auth/logout", headers=headers, follow_redirects=False).status_code == 303
    assert client.get("/room", headers=headers).status_code == 401


# --- Swarm stream ----------------------------------------------------------------------------------

def _event(conn, role, event, detail="", duration=None):
    return conn.execute(
        "INSERT INTO swarm_events (at, sprite_role, event, detail, duration_ms) VALUES (?, ?, ?, ?, ?)",
        (db.now(), role, event, detail, duration),
    ).lastrowid


class _Disconnect:
    """is_disconnected() that turns True after `polls` checks."""

    def __init__(self, polls):
        self.polls, self.checks = polls, 0

    async def __call__(self):
        self.checks += 1
        return self.checks > self.polls


async def test_swarm_stream_sends_new_sanitized_events_and_stops_on_disconnect(tmp_path):
    conn = db.connect(tmp_path / "s.db")
    _event(conn, "gateway", "run", "before the stream")  # backlog: not replayed without Last-Event-ID
    disconnect = _Disconnect(polls=3)
    frames = []
    gen = web.swarm_stream(conn, disconnect, poll=0.01)
    frames.append(await gen.__anext__())  # retry hint
    _event(conn, "scout", "wake", "crawl https://secret.example/terms?u=1 for bob@example.com", 12)
    _event(conn, "scout", "egress_lock", "dns locked", None)
    async for frame in gen:
        frames.append(frame)
    assert disconnect.checks == 4  # stopped as soon as the client went away
    data = [json.loads(f.split("data: ", 1)[1]) for f in frames if "data: " in f]
    assert [d["event"] for d in data] == ["wake", "egress_lock"]
    assert set(data[0]) == {"role", "event", "detail", "duration_ms", "at"}
    blob = "".join(frames)
    assert "secret.example" not in blob and "bob@example.com" not in blob and "before the stream" not in blob
    assert data[0]["duration_ms"] == 12 and data[0]["role"] == "scout"


async def test_swarm_stream_resumes_after_last_event_id_and_closes_cleanly(tmp_path):
    conn = db.connect(tmp_path / "s.db")
    first = _event(conn, "gateway", "wake")
    _event(conn, "advocate", "sleep", "", 300)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(conn=conn)),
        headers={"last-event-id": str(first)},
        is_disconnected=_Disconnect(polls=100),
    )
    resp = await web.swarm(request)
    assert resp.media_type == "text/event-stream" and resp.headers["cache-control"] == "no-cache"
    body = resp.body_iterator
    frames = [await body.__anext__(), await body.__anext__()]
    assert '"event": "sleep"' in frames[1] and f"id: {first + 1}" in frames[1]
    # A client that goes away mid-wait: closing the generator during its sleep ends it without error.
    pending = asyncio.ensure_future(body.__anext__())
    await asyncio.sleep(0.05)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await body.aclose()


def test_radar_renders_the_swarm_strip(client, conn):
    _event(conn, "scout", "checkpoint_restore", "base checkpoint", 310)
    page = client.get("/").text
    assert "Swarm Activity" in page and "checkpoint_restore" in page and "/api/swarm/stream" in page

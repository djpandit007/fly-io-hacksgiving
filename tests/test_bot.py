"""Telegram conversation: claiming, joining, strangers, owner-only commands and member isolation."""

import pytest

from app import agent, bot, db, ledger, users
from app.telegram import Incoming


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.configured = True

    async def send(self, chat, text):
        self.sent.append((chat, text))

    def to(self, chat):
        return [t for c, t in self.sent if c == chat]


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "bot.db")


@pytest.fixture
def tg():
    return FakeTelegram()


async def say(conn, tg, chat, text, name="Someone"):
    await bot.handle(conn, None, tg, Incoming(chat_id=chat, name=name, text=text))


async def test_owner_claims_then_members_join(conn, tg):
    await say(conn, tg, 1, "/start owner-code", name="Dhananjay")
    assert users.owner(conn).chat_id == 1 and "owner" in tg.to(1)[0]
    await say(conn, tg, 2, "/start join-code", name="Judge")
    assert users.by_chat(conn, 2).role == "member"
    assert any("Welcome to <b>Fine Print</b>, Judge" in t for t in tg.to(2))
    assert any("not legal advice" in t for t in tg.to(2))


async def test_stranger_start_is_explained_and_other_messages_ignored(conn, tg):
    await say(conn, tg, 9, "/start")
    await say(conn, tg, 9, "/start wrong-code")
    assert all("private Fine Print bot" in t for t in tg.to(9)) and len(tg.to(9)) == 2
    await say(conn, tg, 9, "https://example.com/terms")
    await say(conn, tg, 9, "/list")
    assert len(tg.to(9)) == 2  # no replies, no work
    assert users.by_chat(conn, 9) is None


async def test_owner_claim_code_cannot_take_an_owned_bot(conn, tg):
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 2, "/start owner-code")
    assert users.owner(conn).chat_id == 1 and users.by_chat(conn, 2) is None


async def test_member_cannot_use_owner_commands(conn, tg):
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/members")
    await say(conn, tg, 2, f"/remove {users.owner(conn).id}")
    assert tg.to(2)[-2:] == ["That command is owner-only.", "That command is owner-only."]
    assert users.owner(conn).status == "active"


async def test_owner_lists_and_removes_members(conn, tg):
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 2, "/start join-code", name="Colleague")
    member = users.by_chat(conn, 2)
    await say(conn, tg, 1, "/members")
    assert "Colleague" in tg.to(1)[-1] and f"<code>{member.id}</code>" in tg.to(1)[-1]
    await say(conn, tg, 1, f"/remove {member.id}")
    assert tg.to(1)[-1] == f"Removed member {member.id}."
    before = len(tg.to(2))
    await say(conn, tg, 2, "/list")
    assert len(tg.to(2)) == before  # removed: not served


async def test_members_see_only_their_own_list_and_deadlines(conn, tg):
    from datetime import date, timedelta

    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 3, "/start join-code")
    a, b = users.by_chat(conn, 2), users.by_chat(conn, 3)
    x = agent.add_service(conn, "Xsvc", "https://x.test/terms")
    y = agent.add_service(conn, "Ysvc", "https://y.test/terms")
    conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (a.id, x.id, db.now()))
    conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (b.id, y.id, db.now()))
    ledger.add(conn, a.id, "A's deadline", date.today() + timedelta(days=5), "notice")

    await say(conn, tg, 2, "/list")
    await say(conn, tg, 3, "/list")
    assert "Xsvc" in tg.to(2)[-1] and "Ysvc" not in tg.to(2)[-1]
    assert "Ysvc" in tg.to(3)[-1] and "Xsvc" not in tg.to(3)[-1]

    await say(conn, tg, 3, "/deadlines")
    assert "A's deadline" not in tg.to(3)[-1]
    await say(conn, tg, 2, "/deadlines")
    assert "A's deadline" in tg.to(2)[-1]


async def test_deadlines_show_the_members_own_calendar_link(conn, tg, monkeypatch):
    monkeypatch.setattr(bot, "settings", bot.settings.model_copy(update={"public_url": "https://fp.test"}))
    await say(conn, tg, 2, "/start join-code")
    member = users.by_chat(conn, 2)
    await say(conn, tg, 2, "/deadlines")
    assert f"https://fp.test/calendar.ics?token={member.calendar_token}" in tg.to(2)[-1]


async def test_joining_twice_keeps_data(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    first = users.by_chat(conn, 2)
    await say(conn, tg, 2, "/start join-code")
    assert users.by_chat(conn, 2).calendar_token == first.calendar_token


async def test_owner_help_lists_owner_commands_member_help_does_not(conn, tg):
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 1, "/help")
    await say(conn, tg, 2, "/help")
    assert "/members" in tg.to(1)[-1] and "/members" not in tg.to(2)[-1]


def test_sender_name_is_parsed():
    from app.telegram import parse_update

    msg = parse_update({"update_id": 1, "message": {"message_id": 1, "chat": {"id": 5},
                        "from": {"id": 5, "first_name": "Ada", "last_name": "L", "username": "ada"}, "text": "hi"}})
    assert msg.name == "Ada L (@ada)"

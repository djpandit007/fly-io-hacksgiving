"""Telegram conversation: claiming, joining, strangers, owner-only commands and member isolation."""

import httpx
import pytest
import respx

from app import agent, bot, db, ledger, users
from app.telegram import Incoming


class FakeTelegram:
    """Records what the bot sends and every other Bot API call it makes."""

    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.messages: list[dict] = []   # every sent message with its buttons, reply_to and markup
        self.calls: list[tuple] = []     # (method, *args) for everything that isn't sendMessage
        self.configured = True
        self._next_id = 100

    async def send(self, chat, text, *, buttons=None, reply_to=None, markup=None):
        self._next_id += 1
        self.sent.append((chat, text))
        self.messages.append({"chat": chat, "text": text, "buttons": buttons, "reply_to": reply_to,
                              "markup": markup, "id": self._next_id})
        return [self._next_id]

    async def answer_callback(self, callback_id, text="", alert=False):
        self.calls.append(("answer", callback_id, text, alert))

    async def edit_buttons(self, chat, message_id, buttons):
        self.calls.append(("edit", chat, message_id, buttons))

    async def react(self, chat, message_id, emoji):
        self.calls.append(("react", chat, message_id, emoji))

    async def draft(self, chat, draft_id, text=""):
        self.calls.append(("draft", chat, text))

    async def typing(self, chat):
        self.calls.append(("typing", chat))

    async def set_commands(self, commands, scope):
        self.calls.append(("commands", commands, scope))

    async def set_description(self, description, short):
        self.calls.append(("description", description, short))

    def to(self, chat):
        return [t for c, t in self.sent if c == chat]

    def last(self, chat) -> dict:
        return [m for m in self.messages if m["chat"] == chat][-1]

    def called(self, method) -> list[tuple]:
        return [c for c in self.calls if c[0] == method]


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "bot.db")


@pytest.fixture
def tg():
    return FakeTelegram()


async def say(conn, tg, chat, text, name="Someone", **fields):
    await bot.handle(conn, None, tg, Incoming(chat_id=chat, name=name, text=text, **fields))


async def test_owner_claims_then_members_join(conn, tg):
    await say(conn, tg, 1, "/start owner-code", name="Dhananjay")
    assert users.owner(conn).chat_id == 1 and "owner" in tg.to(1)[0]
    await say(conn, tg, 2, "/start join-code", name="Judge")
    assert users.by_chat(conn, 2).role == "member"
    assert any("Welcome to <b>Fine Print</b>, Judge" in t for t in tg.to(2))
    assert any("not legal advice" in t for t in tg.to(2))


def test_welcome_says_questions_go_to_ai_providers():
    assert "questions you send are read by AI providers" in " ".join(bot.WELCOME.split())


def test_pointer_mentions_ask():
    assert "/ask" in bot.POINTER


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


async def test_watch_sends_one_reply(conn, tg, monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    await say(conn, tg, 2, "/start join-code")
    svc = agent.add_service(conn, "Svc", "https://svc.test/terms")
    reports = [agent.ChangeReport(change_id=i, service="Svc", heading=f"Heading {i}", kind="modified",
                                  effective_at=datetime(2026, 3, i, tzinfo=UTC), verdict=None,
                                  summary="s", redline="r") for i in (1, 2, 3)]

    async def fake_subscribe(conn_, cl, user, name, url, on_stage=None):
        return svc, reports

    monkeypatch.setattr(agent, "subscribe", fake_subscribe)
    before = len(tg.to(2))
    await bot.handle(conn, SimpleNamespace(ai=None), tg, Incoming(chat_id=2, name="A", text="/watch https://svc.test/terms"))
    sent = tg.to(2)[before:]
    assert len(sent) == 1 and sent[0].startswith("Watching")
    assert all(f"Heading {i}" in sent[0] for i in (1, 2, 3))


async def test_watch_with_no_changes_says_so(conn, tg, monkeypatch):
    from types import SimpleNamespace

    await say(conn, tg, 2, "/start join-code")
    svc = agent.add_service(conn, "Svc", "https://svc.test/terms")

    async def fake_subscribe(conn_, cl, user, name, url, on_stage=None):
        return svc, []

    monkeypatch.setattr(agent, "subscribe", fake_subscribe)
    await bot.handle(conn, SimpleNamespace(ai=None), tg, Incoming(chat_id=2, name="A", text="/watch https://svc.test/terms"))
    assert "No material changes" in tg.to(2)[-1]


CHAT = "https://opencode.test/inference/openai/v1/chat/completions"


@respx.mock
async def test_watch_reply_leads_with_the_overview(conn, tg, monkeypatch):
    from datetime import UTC, datetime

    respx.post(CHAT).mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": "The big one: arbitration."}, "finish_reason": "stop"}]}))
    await say(conn, tg, 2, "/start join-code")
    svc = agent.add_service(conn, "Svc", "https://svc.test/terms")
    reports = [agent.ChangeReport(change_id=i, service="Svc", heading=f"Heading {i}", kind="modified",
                                  effective_at=datetime(2026, 3, i, tzinfo=UTC), verdict=None,
                                  summary="s", redline="r") for i in (1, 2)]

    async def fake_subscribe(conn_, cl, user, name, url, on_stage=None):
        return svc, reports

    monkeypatch.setattr(agent, "subscribe", fake_subscribe)
    cl = agent.Clients()
    await bot.handle(conn, cl, tg, Incoming(chat_id=2, name="A", text="/watch https://svc.test/terms"))
    await cl.aclose()
    reply = tg.to(2)[-1]
    assert reply.index("The big one: arbitration.") < reply.index("Heading 1")


@respx.mock
async def test_watch_with_no_changes_makes_no_overview_call(conn, tg, monkeypatch):
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, json={"choices": []}))
    await say(conn, tg, 2, "/start join-code")
    svc = agent.add_service(conn, "Svc", "https://svc.test/terms")

    async def fake_subscribe(conn_, cl, user, name, url, on_stage=None):
        return svc, []

    monkeypatch.setattr(agent, "subscribe", fake_subscribe)
    cl = agent.Clients()
    await bot.handle(conn, cl, tg, Incoming(chat_id=2, name="A", text="/watch https://svc.test/terms"))
    await cl.aclose()
    assert not route.called
    assert "No material changes" in tg.to(2)[-1]


# --- Progress while working --------------------------------------------------------------------------

async def test_link_check_shows_progress_and_replies_to_the_link(conn, tg, monkeypatch):
    async def fake_check(cl, url, on_stage=None, **kw):
        on_stage("Fetching the page…")
        return "<b>Card</b>"

    monkeypatch.setattr(agent, "check_url", fake_check)
    await say(conn, tg, 2, "/start join-code")
    before = len(tg.to(2))
    await say(conn, tg, 2, "https://acme.test/terms", message_id=55)
    assert tg.to(2)[before:] == ["<b>Card</b>"]  # no "Checking…" placeholder
    assert tg.last(2)["reply_to"] == 55
    assert [c[3] for c in tg.called("react")] == ["👀", "👌"]
    assert any(c[2] == "Fetching the page…" for c in tg.called("draft"))


async def test_failed_check_reacts_sad_and_still_explains(conn, tg, monkeypatch):
    async def broken(cl, url, on_stage=None, **kw):
        raise RuntimeError("page gone")

    monkeypatch.setattr(agent, "check_url", broken)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "https://acme.test/terms", message_id=56)
    assert [c[3] for c in tg.called("react")] == ["👀", "😢"]
    assert tg.to(2)[-1].startswith("Something went wrong")


async def test_file_check_shows_progress_without_placeholder(conn, tg, monkeypatch):
    async def fake_intake(conn_, cl, user, filename, content, on_stage=None):
        return "<b>Lease card</b>"

    async def download(file_id):
        return "lease.pdf", b"%PDF"

    monkeypatch.setattr(agent, "intake_file", fake_intake)
    tg.download = download
    await say(conn, tg, 2, "/start join-code")
    before = len(tg.to(2))
    await say(conn, tg, 2, "", file_id="f1", file_name="lease.pdf", message_id=57)
    assert tg.to(2)[before:] == ["<b>Lease card</b>"] and tg.last(2)["reply_to"] == 57
    assert [c[3] for c in tg.called("react")] == ["👀", "👌"]


# --- Button taps ----------------------------------------------------------------------------------

async def tap(conn, tg, chat, data, message_id=900, cl=None, buttons=None):
    await bot.handle(conn, cl, tg, Incoming(kind="tap", chat_id=chat, data=data, callback_id=f"cb-{len(tg.calls)}",
                                            message_id=message_id, buttons=buttons or []))


def answers(tg):
    return [c[2] for c in tg.called("answer")]


async def test_stranger_tap_gets_a_private_notice_and_nothing_else(conn, tg):
    await tap(conn, tg, 9, "v1:u:1")
    assert answers(tg) == [bot.PRIVATE_TAP] and tg.called("answer")[0][3] is True
    assert tg.sent == [] and not tg.called("edit")


async def test_removed_member_tapping_an_old_button_is_not_served(conn, tg):
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 2, "/start join-code")
    member = users.by_chat(conn, 2)
    svc = agent.add_service(conn, "Notion", "https://notion.test/terms")
    conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (member.id, svc.id, db.now()))
    users.remove(conn, member.id)
    await tap(conn, tg, 2, f"v1:u:{svc.id}")
    assert answers(tg)[-1] == bot.PRIVATE_TAP
    assert conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 1


async def test_unknown_data_gets_a_short_notice(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    before = len(tg.sent)
    await tap(conn, tg, 2, "v9:hack:everything")
    assert answers(tg) == [bot.STALE_TAP] and len(tg.sent) == before


async def test_every_tap_is_answered_exactly_once(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    await tap(conn, tg, 2, "v1:n", buttons=[[("Yes, watch it", "v1:w:notion"), ("No", "v1:n")]])
    assert len(tg.called("answer")) == 1
    assert tg.called("edit") == [("edit", 2, 900, None)]


async def test_taps_are_counted_by_action_without_member_or_data(conn, tg):
    from tests.test_metrics import counts, everything_stored

    await say(conn, tg, 4242, "/start join-code", name="Secret Name")
    await tap(conn, tg, 4242, "v1:n")
    await tap(conn, tg, 4242, "junk")
    assert counts(conn, "tap") == {("no", "ok"): 1, ("unknown", "ok"): 1}
    stored = everything_stored(conn)
    assert "4242" not in stored and "Secret Name" not in stored and "junk" not in stored


# --- Guidance -------------------------------------------------------------------------------------

async def test_setup_publishes_description_and_both_menus(conn, tg):
    await say(conn, tg, 1, "/start owner-code")
    tg.calls.clear()
    await bot.setup(conn, tg)
    (desc,) = tg.called("description")
    assert "private" in desc[1] and "join link" in desc[1] and len(desc[1]) <= 512 and len(desc[2]) <= 120
    scopes = {str(c[2]): [name for name, _ in c[1]] for c in tg.called("commands")}
    member = scopes["{'type': 'all_private_chats'}"]
    owner = scopes["{'type': 'chat', 'chat_id': 1}"]
    assert {"watch", "jurisdiction", "dashboard"} <= set(member) and not {"members", "stats", "remove"} & set(member)
    assert {"members", "stats", "remove"} <= set(owner) and "watch" in owner


async def test_menus_match_the_counted_commands():
    menu = {f"/{c}" for c, _ in bot.MEMBER_MENU + bot.EMAIL_MENU + bot.OWNER_MENU}
    assert bot.COMMANDS == menu | {"/start"}
    assert bot.COMMANDS == {"/start", "/help", "/watch", "/list", "/deadlines", "/email", "/verify", "/wrapped",
                            "/members", "/remove", "/stats", "/ask", "/jurisdiction", "/dashboard"}


async def test_setup_failure_does_not_raise(conn):
    class Broken(FakeTelegram):
        async def set_description(self, description, short):
            raise RuntimeError("Telegram down")

        async def set_commands(self, commands, scope):
            raise RuntimeError("Telegram down")

    await bot.setup(conn, Broken())  # no exception


async def test_claiming_ownership_sets_the_owner_menu(conn, tg):
    await say(conn, tg, 1, "/start owner-code")
    (call,) = tg.called("commands")
    assert call[2] == {"type": "chat", "chat_id": 1} and "stats" in [c for c, _ in call[1]]


async def test_member_claiming_ownership_later_gets_the_owner_menu(conn, tg):
    await say(conn, tg, 1, "/start join-code")
    await say(conn, tg, 1, "/start owner-code")
    assert users.owner(conn).chat_id == 1
    assert tg.called("commands")[-1][2] == {"type": "chat", "chat_id": 1}


async def test_help_and_welcome_carry_buttons(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    assert tg.last(2)["buttons"] == bot.HELP_BUTTONS
    await say(conn, tg, 2, "/help")
    assert tg.last(2)["buttons"] == bot.HELP_BUTTONS


async def test_help_buttons_do_what_the_commands_do(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    member = users.by_chat(conn, 2)
    svc = agent.add_service(conn, "Xsvc", "https://x.test/terms")
    conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (member.id, svc.id, db.now()))
    await say(conn, tg, 2, "/list")
    await tap(conn, tg, 2, "v1:h:list")
    assert tg.to(2)[-1] == tg.to(2)[-2]
    await say(conn, tg, 2, "/deadlines")
    await tap(conn, tg, 2, "v1:h:dl")
    assert tg.to(2)[-1] == tg.to(2)[-2]
    await tap(conn, tg, 2, "v1:h:how")
    assert all(w in tg.to(2)[-1] for w in ("link", "PDF", "screenshot", "photo", "before you agree", "redline kit"))


async def test_help_publishes_jurisdiction_and_dashboard_with_a_control_room_button(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/help")
    assert "/jurisdiction" in tg.to(2)[-1] and "/dashboard" in tg.to(2)[-1] and "/members" not in tg.to(2)[-1]
    labels = {label: data for row in bot.HELP_BUTTONS for label, data in row}
    assert labels["🛰 Control Room"] == "v1:h:room"


async def test_control_room_button_does_what_dashboard_does(conn, tg, monkeypatch):
    from app import sessions

    monkeypatch.setattr(sessions, "settings", sessions.settings.model_copy(update={"public_url": "https://fp.test"}))
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/dashboard")
    by_command = tg.to(2)[-1]
    await tap(conn, tg, 2, "v1:h:room")
    by_button = tg.to(2)[-1]
    for text in (by_command, by_button):
        assert "https://fp.test/auth/magic?token=" in text and "expires in 10 minutes" in text
    assert by_command != by_button  # each is a fresh single-use link
    # Only the latest link is live, and only its hash is stored.
    member = users.by_chat(conn, 2)
    stored = conn.execute("SELECT token_hash FROM dashboard_sessions WHERE user_id = ?", (member.id,)).fetchall()
    assert len(stored) == 1 and stored[0][0] not in by_button


async def test_dashboard_without_a_public_url_says_so(conn, tg, monkeypatch):
    from app import sessions

    monkeypatch.setattr(sessions, "settings", sessions.settings.model_copy(update={"public_url": ""}))
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/dashboard")
    assert tg.to(2)[-1] == bot.DASHBOARD_OFF
    assert conn.execute("SELECT COUNT(*) FROM dashboard_sessions").fetchone()[0] == 0


async def test_watch_without_a_name_asks_which_service(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/watch")
    assert tg.to(2)[-1] == bot.WATCH_PROMPT
    assert tg.last(2)["markup"] == {"force_reply": True, "input_field_placeholder": bot.WATCH_PLACEHOLDER}
    await tap(conn, tg, 2, "v1:h:watch")
    assert tg.to(2)[-1] == bot.WATCH_PROMPT


async def test_reply_to_the_prompt_watches_but_a_plain_message_does_not(conn, tg, monkeypatch):
    watched = []

    async def fake_watch(conn_, cl, tg_, user, arg, reply_to):
        watched.append(arg)

    monkeypatch.setattr(bot, "_watch", fake_watch)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "notion", reply_to_text=bot.WATCH_PROMPT)
    assert watched == ["notion"]
    await say(conn, tg, 2, "notion")
    assert watched == ["notion"] and tg.to(2)[-1] == "Watch <b>Notion</b>?"


async def test_free_text_gets_a_suggestion_or_a_pointer(conn, tg, monkeypatch):
    asked = fake_ask(monkeypatch)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "youtube.com")
    assert tg.to(2)[-1] == "Watch <b>Youtube</b>?"
    assert tg.last(2)["buttons"] == [[("Yes, watch it", "v1:w:youtube.com"), ("No", "v1:n")]]
    await say(conn, tg, 2, "what does this bot do?")
    assert asked == ["what does this bot do?"]  # a free-text question is answered from the terms
    await say(conn, tg, 2, "x" * 59)
    assert tg.to(2)[-1] == bot.POINTER
    await say(conn, tg, 2, "thanks")
    assert tg.to(2)[-1] == bot.POINTER


async def test_yes_watches_once_and_removes_the_buttons(conn, tg, monkeypatch):
    watched = []

    async def fake_watch(conn_, cl, tg_, user, arg, reply_to):
        watched.append((arg, reply_to))

    monkeypatch.setattr(bot, "_watch", fake_watch)
    await say(conn, tg, 2, "/start join-code")
    await tap(conn, tg, 2, "v1:w:notion", message_id=321, buttons=[[("Yes, watch it", "v1:w:notion"), ("No", "v1:n")]])
    assert watched == [("notion", 321)]
    assert ("edit", 2, 321, None) in tg.calls and answers(tg) == ["On it…"]


async def test_no_removes_the_buttons(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    await tap(conn, tg, 2, "v1:n", message_id=321, buttons=[[("Yes, watch it", "v1:w:notion"), ("No", "v1:n")]])
    assert ("edit", 2, 321, None) in tg.calls


async def test_stranger_typing_a_service_name_gets_no_reply(conn, tg):
    await say(conn, tg, 9, "notion")
    assert tg.sent == []


async def test_failed_watch_tap_says_so_in_the_chat(conn, tg, monkeypatch):
    async def broken(conn_, cl, tg_, user, arg, reply_to):
        raise RuntimeError("discovery down")

    monkeypatch.setattr(bot, "_watch", broken)
    await say(conn, tg, 2, "/start join-code")
    await tap(conn, tg, 2, "v1:w:notion")
    assert tg.to(2)[-1].startswith("Something went wrong")


# --- Unwatch and undo -----------------------------------------------------------------------------

def _watching(conn, chat, name="Notion"):
    member = users.by_chat(conn, chat)
    svc = agent.add_service(conn, name, f"https://{name.lower()}.test/terms")
    conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (member.id, svc.id, db.now()))
    return member, svc


async def test_list_has_an_unwatch_button_per_service(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    _, a = _watching(conn, 2, "Notion")
    _, b = _watching(conn, 2, "Spotify")
    await say(conn, tg, 2, "/list")
    assert tg.last(2)["buttons"] == [[("🗑 Unwatch Notion", f"v1:u:{a.id}")], [("🗑 Unwatch Spotify", f"v1:u:{b.id}")]]


async def test_unwatch_from_list_swaps_in_undo_and_undo_restores(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    member, a = _watching(conn, 2, "Notion")
    _, b = _watching(conn, 2, "Spotify")
    shown = [[("🗑 Unwatch Notion", f"v1:u:{a.id}")], [("🗑 Unwatch Spotify", f"v1:u:{b.id}")]]
    await tap(conn, tg, 2, f"v1:u:{a.id}", message_id=50, buttons=shown)
    assert answers(tg)[-1] == "Stopped watching Notion"
    assert [s.name for s in agent.services_for(conn, member.id)] == ["Spotify"]
    edited = tg.called("edit")[-1]
    assert edited[2] == 50 and edited[3] == [[("↩ Undo: watch Notion", f"v1:r:{a.id}")], shown[1]]

    await tap(conn, tg, 2, f"v1:r:{a.id}", message_id=50, buttons=edited[3])
    assert answers(tg)[-1] == "Watching Notion again"
    assert [s.name for s in agent.services_for(conn, member.id)] == ["Notion", "Spotify"]
    assert tg.called("edit")[-1][3] == shown


async def test_unwatching_twice_says_nothing_changed(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    _, svc = _watching(conn, 2)
    await tap(conn, tg, 2, f"v1:u:{svc.id}")
    await tap(conn, tg, 2, f"v1:u:{svc.id}")
    assert answers(tg)[-1] == "You're not watching that, so nothing changed."


async def test_forged_unwatch_cannot_touch_another_members_watchlist(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 3, "/start join-code")
    b, svc = _watching(conn, 3, "OnlyB")
    await tap(conn, tg, 2, f"v1:u:{svc.id}")
    assert answers(tg)[-1] == "You're not watching that, so nothing changed."
    assert [s.name for s in agent.services_for(conn, b.id)] == ["OnlyB"]


async def test_undo_for_a_missing_service(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    await tap(conn, tg, 2, "v1:r:999999")
    assert answers(tg)[-1] == "That service can't be restored."


async def test_forged_undo_tap_reveals_nothing(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 3, "/start join-code")
    _, svc = _watching(conn, 3, "Secretsvc")
    await tap(conn, tg, 2, f"v1:r:{svc.id}")
    assert answers(tg)[-1] == "That service can't be restored."
    assert agent.services_for(conn, users.by_chat(conn, 2).id) == []


async def test_undo_uses_no_ai(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    member, svc = _watching(conn, 2)
    await tap(conn, tg, 2, f"v1:u:{svc.id}")
    await tap(conn, tg, 2, f"v1:r:{svc.id}", cl=None)  # no clients at all: nothing to call
    assert [s.name for s in agent.services_for(conn, member.id)] == ["Notion"]


async def test_watch_result_has_an_unwatch_button_on_its_last_message(conn, tg, monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    await say(conn, tg, 2, "/start join-code")
    svc = agent.add_service(conn, "Svc", "https://svc.test/terms")
    reports = [agent.ChangeReport(change_id=i, service="Svc", heading="H" * 1500 + str(i), kind="modified",
                                  effective_at=datetime(2026, 3, i, tzinfo=UTC), verdict=None,
                                  summary="s", redline="r") for i in (1, 2, 3, 4)]

    async def fake_subscribe(conn_, cl, user, name, url, on_stage=None):
        return svc, reports

    monkeypatch.setattr(agent, "subscribe", fake_subscribe)
    before = len(tg.messages)
    await bot.handle(conn, SimpleNamespace(ai=None), tg, Incoming(chat_id=2, text="/watch https://svc.test/terms"))
    sent = tg.messages[before:]
    assert len(sent) > 1 and all(m["buttons"] is None for m in sent[:-1])
    assert sent[-1]["buttons"] == [[("🗑 Unwatch Svc", f"v1:u:{svc.id}")]]


# --- Explaining flags -----------------------------------------------------------------------------

async def test_explain_tap_shows_the_explanation_as_a_popup_only(conn, tg):
    from app.judge import FLAG_EXPLANATIONS

    await say(conn, tg, 2, "/start join-code")
    before = len(tg.sent)
    await tap(conn, tg, 2, "v1:fx:arbitration", cl=None)  # no clients: nothing could be called
    assert tg.called("answer")[-1][2:] == (FLAG_EXPLANATIONS["arbitration"], True)
    assert len(tg.sent) == before


async def test_link_check_card_keeps_its_explain_buttons(conn, tg, monkeypatch):
    from app.outgoing import Outgoing

    async def fake_check(cl, url, on_stage=None, **kw):
        return Outgoing(text="<b>Card</b>", buttons=[[("💡 arbitration", "v1:fx:arbitration")]])

    monkeypatch.setattr(agent, "check_url", fake_check)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "https://acme.test/terms", message_id=60)
    assert tg.last(2)["buttons"] == [[("💡 arbitration", "v1:fx:arbitration")]] and tg.last(2)["reply_to"] == 60


# --- Answer feedback ------------------------------------------------------------------------------

def _verdict(**kw):
    from app.outgoing import Answer, Outgoing

    return Outgoing(text="<b>Card</b>", buttons=[[("💡 arbitration", "v1:fx:arbitration")]],
                    rate=Answer(kind="verdict", model="m", prompt_version="v2", quotes_ok=True), **kw)


async def _rated_check(conn, tg, monkeypatch):
    async def fake_check(cl, url, on_stage=None, **kw):
        return _verdict()

    monkeypatch.setattr(agent, "check_url", fake_check)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "https://private.example/lease?token=s3cret", message_id=70)
    buttons = tg.last(2)["buttons"]
    token = buttons[-1][0][1].split(":")[2]
    return buttons, token


async def test_rated_answer_gets_its_own_rating_row(conn, tg, monkeypatch):
    buttons, token = await _rated_check(conn, tg, monkeypatch)
    assert buttons == [[("💡 arbitration", "v1:fx:arbitration")],
                       [("👍", f"v1:fb:{token}:u"), ("👎", f"v1:fb:{token}:d")]]


async def test_lists_and_help_are_not_rated(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/list")
    await say(conn, tg, 2, "/help")
    for m in tg.messages:
        assert not any("v1:fb:" in d for row in (m["buttons"] or []) for _, d in row)


async def test_thumbs_up_says_thanks(conn, tg, monkeypatch):
    buttons, token = await _rated_check(conn, tg, monkeypatch)
    await tap(conn, tg, 2, f"v1:fb:{token}:u", message_id=71, buttons=buttons)
    assert answers(tg)[-1] == "Thanks!"
    assert tg.called("edit")[-1][3] == [buttons[0], [("✓ Thanks", "v1:k")]]
    assert conn.execute("SELECT vote FROM answers WHERE token = ?", (token,)).fetchone()[0] == "up"


async def test_thumbs_down_offers_reasons_then_thanks(conn, tg, monkeypatch):
    buttons, token = await _rated_check(conn, tg, monkeypatch)
    await tap(conn, tg, 2, f"v1:fb:{token}:d", message_id=71, buttons=buttons)
    reasons = tg.called("edit")[-1][3]
    assert [label for label, _ in reasons[-1]] == ["Wrong", "Unclear", "Not useful"] and reasons[0] == buttons[0]
    await tap(conn, tg, 2, f"v1:fr:{token}:c", message_id=71, buttons=reasons)
    assert answers(tg)[-1] == "Thanks, noted."
    thanked = tg.called("edit")[-1][3]
    assert thanked == [buttons[0], [("✓ Thanks", "v1:k")]]
    edits = len(tg.called("edit"))
    await tap(conn, tg, 2, "v1:k", message_id=71, buttons=thanked)  # tapping Thanks again
    assert answers(tg)[-1] == "Already noted. Thanks!" and len(tg.called("edit")) == edits
    row = conn.execute("SELECT vote, reason FROM answers WHERE token = ?", (token,)).fetchone()
    assert tuple(row) == ("down", "unclear")


async def test_changing_a_vote_keeps_one_vote(conn, tg, monkeypatch):
    buttons, token = await _rated_check(conn, tg, monkeypatch)
    await tap(conn, tg, 2, f"v1:fb:{token}:u", buttons=buttons)
    await tap(conn, tg, 2, f"v1:fb:{token}:d", buttons=buttons)
    assert conn.execute("SELECT COUNT(*), MAX(vote) FROM answers").fetchone()[:] == (1, "down")


async def test_forged_token_is_refused(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    await tap(conn, tg, 2, "v1:fb:AAAAAAAAAAAA:u")
    assert answers(tg)[-1] == "This answer can't be rated any more."
    assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0


async def test_votes_store_no_text_url_or_member(conn, tg, monkeypatch):
    from tests.test_metrics import counts

    buttons, token = await _rated_check(conn, tg, monkeypatch)
    await tap(conn, tg, 2, f"v1:fb:{token}:u", buttons=buttons)
    stored = " ".join(str(v) for r in conn.execute("SELECT * FROM answers") for v in tuple(r))
    for private in ("Card", "private.example", "s3cret", "Someone"):
        assert private not in stored
    assert counts(conn, "tap") == {}  # votes are recorded by feedback, not counted again


async def test_alerts_are_rated_in_telegram_but_not_by_email(conn, tg):
    from app import notify
    from app.outgoing import Answer, Outgoing

    chat_member = users.join_member(conn, "join-code", chat_id=7)
    email_member = users.join_member(conn, "join-code", email="judge@example.com")
    out = Outgoing(text="<b>alert</b>", buttons=[[("🗑 Unwatch A", "v1:u:1")]],
                   rate=Answer(kind="alert", model="m", prompt_version="v1", change_id=3))

    class NoMail:
        mail = None

    await notify.deliver(conn, tg, NoMail(), {chat_member.id: [out], email_member.id: [out]}, "Subject")
    assert [label for label, _ in tg.last(7)["buttons"][-1]] == ["👍", "👎"]
    assert conn.execute("SELECT COUNT(*), MAX(change_id) FROM answers").fetchone()[:] == (1, 3)


async def test_wrapped_is_rated(conn, tg, monkeypatch):
    from app.outgoing import Answer, Outgoing

    async def fake_wrapped(conn_, cl, user):
        return Outgoing(text="<b>Wrapped</b>", rate=Answer(kind="wrapped", model="m", prompt_version="v1"))

    monkeypatch.setattr(agent, "wrapped", fake_wrapped)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/wrapped", message_id=80)
    assert [label for label, _ in tg.last(2)["buttons"][-1]] == ["👍", "👎"]


# --- Asking about terms ---------------------------------------------------------------------------

def fake_ask(monkeypatch, outcome="not_found", html="I couldn't find that in the terms you watch.", raises=None,
             service=""):
    """Patch `ask.answer`; returns the questions it was asked."""
    from app import ask

    asked: list[str] = []

    async def answer(conn_, cl, user, question, on_stage=lambda _: None):
        asked.append(question)
        on_stage("Finding the clauses…")
        if raises:
            raise raises
        return ask.AskResult(html=html, outcome=outcome, service=service)

    monkeypatch.setattr(ask, "answer", answer)
    return asked


async def test_ask_command_replies_threaded(conn, tg, monkeypatch):
    asked = fake_ask(monkeypatch, outcome="answered", html="<b>Yes</b>, with notice.")
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/ask can spotify raise prices?", message_id=90)
    assert asked == ["can spotify raise prices?"]
    assert tg.last(2)["text"] == "<b>Yes</b>, with notice." and tg.last(2)["reply_to"] == 90
    assert [c[3] for c in tg.called("react")] == ["👀", "👌"]
    assert any(c[2] == "Finding the clauses…" for c in tg.called("draft"))


async def test_bare_ask_explains_usage(conn, tg, monkeypatch):
    asked = fake_ask(monkeypatch)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/ask")
    assert tg.to(2)[-1] == (
        "Ask me about the terms you watch, for example: /ask can Spotify raise my price?\n"
        "Your questions are read by the AI providers and not stored."
    )
    assert asked == []


async def test_statement_is_not_sent_to_ask(conn, tg, monkeypatch):
    asked = fake_ask(monkeypatch)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "my password is hunter2")
    assert tg.to(2)[-1] == bot.POINTER and tg.last(2)["buttons"] == bot.HELP_BUTTONS
    assert asked == []


async def test_ask_command_skips_the_question_check(conn, tg, monkeypatch):
    asked = fake_ask(monkeypatch)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/ask spotify refunds")
    assert asked == ["spotify refunds"]


async def test_free_text_question_goes_to_ask(conn, tg, monkeypatch):
    asked = fake_ask(monkeypatch)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "can spotify raise my price", message_id=91)
    assert asked == ["can spotify raise my price"]
    assert tg.to(2)[-1] == "I couldn't find that in the terms you watch." and tg.last(2)["reply_to"] == 91


async def test_service_name_still_suggests_watch(conn, tg, monkeypatch):
    asked = fake_ask(monkeypatch)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "spotify")
    assert tg.to(2)[-1] == "Watch <b>Spotify</b>?"
    assert tg.last(2)["buttons"] == [[("Yes, watch it", "v1:w:spotify"), ("No", "v1:n")]]
    assert asked == []


async def test_greeting_gets_pointer_not_ask(conn, tg, monkeypatch):
    asked = fake_ask(monkeypatch)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "thanks")
    assert tg.to(2)[-1] == bot.POINTER and tg.last(2)["buttons"] == bot.HELP_BUTTONS
    assert asked == []


async def test_stranger_question_is_ignored(conn, tg, monkeypatch):
    asked = fake_ask(monkeypatch)
    await say(conn, tg, 9, "/ask can spotify raise prices?")
    await say(conn, tg, 9, "can spotify raise my price")
    assert tg.sent == [] and tg.calls == [] and asked == []


async def test_ask_metric_has_outcome_and_no_text(conn, tg, monkeypatch):
    from tests.test_metrics import counts, everything_stored

    fake_ask(monkeypatch, outcome="not_found")
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/ask can spotify raise prices?")
    await say(conn, tg, 2, "does notion train models on my documents")
    fake_ask(monkeypatch, outcome="answered", html="Yes.")
    await say(conn, tg, 2, "/ask will dropbox renew automatically?")
    command = {k: n for k, n in counts(conn, "command").items() if k[0] == "/ask"}
    assert command == {("/ask", "not_found"): 2, ("/ask", "ok"): 1}
    stored = everything_stored(conn).lower()
    for word in ("spotify", "raise", "prices", "notion", "train", "models", "documents", "dropbox", "renew",
                 "automatically"):
        assert word not in stored


async def test_ask_exception_reacts_sad(conn, tg, monkeypatch, caplog):
    from tests.test_metrics import counts

    fake_ask(monkeypatch, raises=RuntimeError("failed on: can spotify raise prices?"))
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/ask can spotify raise prices?", message_id=92)
    assert [c[3] for c in tg.called("react")] == ["👀", "😢"]
    assert tg.to(2)[-1] == bot.ASK_FAILED
    assert "spotify" not in caplog.text
    assert counts(conn, "command")[("/ask", "error")] == 1


async def test_ask_error_outcome_reacts_sad(conn, tg, monkeypatch):
    # answer() reports a failure as an outcome rather than raising; the reaction must still say so.
    from app import ask

    fake_ask(monkeypatch, outcome="error", html=ask.ERROR)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/ask can spotify raise prices?", message_id=93)
    assert [c[3] for c in tg.called("react")] == ["👀", "😢"]
    assert tg.to(2)[-1] == ask.ERROR


async def test_ask_answers_are_rated_and_store_no_text_or_member(conn, tg, monkeypatch):
    fake_ask(monkeypatch, outcome="answered", html="<b>Yes</b>.")
    await say(conn, tg, 4242, "/start join-code", name="Secret Name")
    await say(conn, tg, 4242, "/ask can spotify raise prices?", message_id=93)
    assert [label for label, _ in tg.last(4242)["buttons"][-1]] == ["👍", "👎"]
    fake_ask(monkeypatch, outcome="quotes_only", html="<b>Here is what the terms say</b>")
    await say(conn, tg, 4242, "/ask can spotify raise prices?")
    assert [label for label, _ in tg.last(4242)["buttons"][-1]] == ["👍", "👎"]
    rows = conn.execute("SELECT * FROM answers").fetchall()
    assert [(r["kind"], r["quotes_ok"]) for r in rows] == [("ask", 1), ("ask", 1)]
    stored = " ".join(str(v) for r in rows for v in tuple(r))
    for private in ("spotify", "Yes", "Here is", "4242", "Secret Name"):
        assert private not in stored
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(answers)")}
    assert not columns & {"text", "question", "url", "user_id", "chat_id", "member"}


async def test_unanswered_asks_are_not_rated(conn, tg, monkeypatch):
    await say(conn, tg, 2, "/start join-code")
    from tests.test_metrics import counts

    for outcome in ("not_found", "no_services", "error", "capped"):
        fake_ask(monkeypatch, outcome=outcome, html="Nothing.")
        await say(conn, tg, 2, "/ask can spotify raise prices?")
        assert tg.last(2)["buttons"] is None
    # not_watching carries only the watch offer, never a rating row
    fake_ask(monkeypatch, outcome="not_watching", html=NOT_WATCHING_SPOTIFY, service="Spotify")
    await say(conn, tg, 2, "/ask can spotify raise prices?")
    assert [[data for _, data in row] for row in tg.last(2)["buttons"]] == [["v1:w:Spotify", "v1:n"]]
    assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0
    assert counts(conn, "command")[("/ask", "capped")] == 1


NOT_WATCHING_SPOTIFY = "You're not watching Spotify, so I haven't read its terms. Watch it and ask again."


async def test_ask_about_unwatched_service_offers_to_watch(conn, tg, monkeypatch):
    from app import taps
    from tests.test_metrics import counts

    fake_ask(monkeypatch, outcome="not_watching", html=NOT_WATCHING_SPOTIFY, service="Spotify")
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/ask can spotify raise my price mid-year?", message_id=96)
    assert tg.last(2)["text"] == NOT_WATCHING_SPOTIFY and tg.last(2)["reply_to"] == 96
    assert tg.last(2)["buttons"] == [[("Yes, watch it", taps.build("w", "Spotify")), ("No", taps.build("n"))]]
    assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0  # no feedback row
    assert counts(conn, "command")[("/ask", "not_watching")] == 1


async def test_not_watching_with_a_name_no_button_can_carry_has_no_buttons(conn, tg, monkeypatch):
    fake_ask(monkeypatch, outcome="not_watching", html="You're not watching it.", service="not a name")
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/ask can it raise my price?")
    assert tg.last(2)["text"] == "You're not watching it." and tg.last(2)["buttons"] is None


async def test_tapping_watch_after_not_watching_runs_the_watch_flow(conn, tg, monkeypatch):
    watched = []

    async def fake_watch(conn_, cl, tg_, user, arg, reply_to):
        watched.append((arg, reply_to))

    monkeypatch.setattr(bot, "_watch", fake_watch)
    fake_ask(monkeypatch, outcome="not_watching", html=NOT_WATCHING_SPOTIFY, service="Spotify")
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/ask can spotify raise my price mid-year?")
    buttons = tg.last(2)["buttons"]
    yes = buttons[0][0][1]
    await tap(conn, tg, 2, yes, message_id=321, buttons=buttons)
    assert watched == [("Spotify", 321)]
    assert ("edit", 2, 321, None) in tg.calls and answers(tg) == ["On it…"]


async def test_long_ask_answer_never_splits_a_blockquote(conn, tg, monkeypatch):
    from app import ask
    from app.ask_tools import Clause
    from app.telegram import MAX_LEN, split

    clauses = [Clause(service=f"Svc{i}", heading=f"Heading {i}", quote="q" * 400) for i in range(ask.MAX_CLAUSES)]
    labels = [f"Svc{i} · Heading {i}" for i in range(ask.MAX_CLAUSES)]
    # Longer than any rendered answer can be, to show the send path still never splits the quotes.
    long_html = "\n\n".join(["s" * 1500, "p" * 1500, ask._quotes(clauses, labels), ask.DISCLAIMER])
    assert len(long_html) > MAX_LEN
    fake_ask(monkeypatch, outcome="answered", html=long_html)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/ask can spotify raise prices?", message_id=94)
    chunks = split(tg.last(2)["text"])
    assert len(chunks) > 1 and all(len(c) <= MAX_LEN for c in chunks)
    assert all(c.count("<blockquote") == c.count("</blockquote>") for c in chunks)


async def test_multi_paragraph_quotes_never_split_a_blockquote(conn, tg, monkeypatch):
    from app import ask
    from app.ask_tools import Clause
    from app.telegram import MAX_LEN, split

    # Quotes that cross paragraph breaks in the stored terms (verification ignores whitespace).
    quote = "\n\n".join(["p" * 60] * 6)
    clauses = [Clause(service=f"Svc{i}", heading=f"Heading {i}", quote=quote) for i in range(ask.MAX_CLAUSES)]
    labels = [f"Svc{i} · Heading {i}" for i in range(ask.MAX_CLAUSES)]
    long_html = "\n\n".join(["s" * 1500, "p" * 1500, ask._quotes(clauses, labels), ask.DISCLAIMER])
    assert len(long_html) > MAX_LEN
    fake_ask(monkeypatch, outcome="answered", html=long_html)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/ask can spotify raise prices?", message_id=95)
    chunks = split(tg.last(2)["text"])
    assert len(chunks) > 1 and all(len(c) <= MAX_LEN for c in chunks)
    assert all(c.count("<blockquote") == c.count("</blockquote>") for c in chunks)


async def test_unchanged_buttons_are_not_edited(conn, tg, monkeypatch):
    buttons, token = await _rated_check(conn, tg, monkeypatch)
    await tap(conn, tg, 2, f"v1:fb:{token}:u", buttons=buttons)
    thanked = tg.called("edit")[-1][3]
    edits = len(tg.called("edit"))
    await tap(conn, tg, 2, f"v1:fb:{token}:u", buttons=thanked)  # an old 👍 data with no rating row left
    assert len(tg.called("edit")) == edits  # Telegram would refuse a no-op edit


# --- Jurisdiction ---------------------------------------------------------------------------------

async def test_jurisdiction_is_set_per_member_and_cleared(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 3, "/start join-code")
    await say(conn, tg, 2, "/jurisdiction us-ca")
    assert users.by_chat(conn, 2).jurisdiction == "US-CA" and users.by_chat(conn, 3).jurisdiction is None
    assert "Saved <b>US-CA</b>" in tg.to(2)[-1] and "California" in tg.to(2)[-1]
    await say(conn, tg, 2, "/jurisdiction")
    assert "Your jurisdiction is <b>US-CA</b>" in tg.to(2)[-1]
    await say(conn, tg, 2, "/jurisdiction clear")
    assert users.by_chat(conn, 2).jurisdiction is None and "Cleared" in tg.to(2)[-1]


async def test_invalid_jurisdiction_is_rejected_with_the_supported_codes(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/jurisdiction EU")  # set first: a rejected code must not clear it
    await say(conn, tg, 2, "/jurisdiction MARS")
    reply = tg.to(2)[-1]
    assert "I don't know the jurisdiction MARS" in reply
    assert all(f"<code>{c}</code>" in reply for c in ("US-CA", "US-NY", "US-OTHER", "EU", "UK"))
    assert users.by_chat(conn, 2).jurisdiction == "EU"


async def test_link_check_passes_the_member_to_the_check(conn, tg, monkeypatch):
    seen = []

    async def fake_check(cl, url, on_stage=None, *, conn=None, user=None):
        seen.append(user.chat_id)
        return "<b>Card</b>"

    monkeypatch.setattr(agent, "check_url", fake_check)
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "https://acme.test/terms")
    assert seen == [2]

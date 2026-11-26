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
    async def fake_check(cl, url, on_stage=None):
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
    async def broken(cl, url, on_stage=None):
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
    await tap(conn, tg, 2, "v1:n")
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
    assert "watch" in member and not {"members", "stats", "remove"} & set(member)
    assert {"members", "stats", "remove"} <= set(owner) and "watch" in owner


async def test_menus_match_the_counted_commands():
    menu = {f"/{c}" for c, _ in bot.MEMBER_MENU + bot.EMAIL_MENU + bot.OWNER_MENU}
    assert bot.COMMANDS == menu | {"/start"}
    assert bot.COMMANDS == {"/start", "/help", "/watch", "/list", "/deadlines", "/email", "/verify", "/wrapped",
                            "/members", "/remove", "/stats"}


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
    assert all(w in tg.to(2)[-1] for w in ("link", "PDF", "screenshot", "photo"))


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


async def test_free_text_gets_a_suggestion_or_a_pointer(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "youtube.com")
    assert tg.to(2)[-1] == "Watch <b>Youtube</b>?"
    assert tg.last(2)["buttons"] == [[("Yes, watch it", "v1:w:youtube.com"), ("No", "v1:n")]]
    await say(conn, tg, 2, "what does this bot do?")
    assert tg.to(2)[-1] == bot.POINTER and tg.last(2)["buttons"] == bot.HELP_BUTTONS
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
    await tap(conn, tg, 2, "v1:w:notion", message_id=321)
    assert watched == [("notion", 321)]
    assert ("edit", 2, 321, None) in tg.calls and answers(tg) == ["On it…"]


async def test_no_removes_the_buttons(conn, tg):
    await say(conn, tg, 2, "/start join-code")
    await tap(conn, tg, 2, "v1:n", message_id=321)
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

    async def fake_check(cl, url, on_stage=None):
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
    async def fake_check(cl, url, on_stage=None):
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
    assert tg.called("edit")[-1][3] == [buttons[0], [("✓ Thanks", f"v1:fb:{token}:u")]]
    assert conn.execute("SELECT vote FROM answers WHERE token = ?", (token,)).fetchone()[0] == "up"


async def test_thumbs_down_offers_reasons_then_thanks(conn, tg, monkeypatch):
    buttons, token = await _rated_check(conn, tg, monkeypatch)
    await tap(conn, tg, 2, f"v1:fb:{token}:d", message_id=71, buttons=buttons)
    reasons = tg.called("edit")[-1][3]
    assert [label for label, _ in reasons[-1]] == ["Wrong", "Unclear", "Not useful"] and reasons[0] == buttons[0]
    await tap(conn, tg, 2, f"v1:fr:{token}:c", message_id=71, buttons=reasons)
    assert answers(tg)[-1] == "Thanks, noted."
    assert tg.called("edit")[-1][3] == [buttons[0], [("✓ Thanks", f"v1:fb:{token}:d")]]
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

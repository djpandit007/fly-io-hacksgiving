import json

import httpx
import pytest
import respx

from app.telegram import API, MAX_LEN, Telegram, parse_update, rich


def test_text_message():
    msg = parse_update({"update_id": 1, "message": {"message_id": 5, "chat": {"id": 42}, "text": "/list"}})
    assert msg and msg.chat_id == 42 and msg.text == "/list" and msg.file_id is None


def test_photo_picks_largest():
    msg = parse_update({"update_id": 1, "message": {"message_id": 5, "chat": {"id": 42}, "caption": "trial?",
        "photo": [{"file_id": "big", "width": 1280, "height": 720}, {"file_id": "small", "width": 90, "height": 60}]}})
    assert msg.file_id == "big" and msg.text == "trial?"


def test_document():
    msg = parse_update({"update_id": 1, "message": {"message_id": 5, "chat": {"id": 42},
        "document": {"file_id": "d1", "file_name": "lease.pdf"}}})
    assert (msg.file_id, msg.file_name) == ("d1", "lease.pdf")


def test_malformed_callback_query_is_ignored():
    assert parse_update({"update_id": 1, "callback_query": {}}) is None


def test_unknown_update_kind_is_ignored():
    assert parse_update({"update_id": 1, "my_chat_member": {}}) is None


def test_tap_is_parsed():
    msg = parse_update({"update_id": 1, "callback_query": {
        "id": "cb1", "from": {"id": 42, "first_name": "Ada"}, "data": "v1:h:list",
        "message": {"message_id": 7, "chat": {"id": 42}, "date": 0}}})
    assert (msg.kind, msg.chat_id, msg.callback_id, msg.data, msg.message_id) == ("tap", 42, "cb1", "v1:h:list", 7)


def test_tap_carries_the_messages_current_buttons():
    msg = parse_update({"update_id": 1, "callback_query": {
        "id": "cb1", "from": {"id": 42}, "data": "v1:u:3",
        "message": {"message_id": 7, "chat": {"id": 42}, "date": 1, "reply_markup": {"inline_keyboard": [
            [{"text": "🗑 Unwatch A", "callback_data": "v1:u:3"}, {"text": "site", "url": "https://x.test"}]]}}}})
    assert msg.buttons == [[("🗑 Unwatch A", "v1:u:3")]]


def test_tap_without_message_uses_the_tapper_chat():
    msg = parse_update({"update_id": 1, "callback_query": {"id": "cb", "from": {"id": 9}, "data": "v1:n"}})
    assert msg.chat_id == 9 and msg.message_id is None


def test_reply_to_a_bot_message_carries_its_text():
    msg = parse_update({"update_id": 1, "message": {"message_id": 6, "chat": {"id": 42}, "text": "notion",
        "reply_to_message": {"message_id": 5, "chat": {"id": 42}, "text": "Which service?"}}})
    assert msg.kind == "message" and msg.message_id == 6 and msg.reply_to_text == "Which service?"


def test_rich_converts_light_markdown_safely():
    from app.telegram import rich

    out = rich("SIGN <now>\n- **Auto-renewal:** bills `monthly` & more")
    assert out == "SIGN &lt;now&gt;\n• <b>Auto-renewal:</b> bills <code>monthly</code> &amp; more"


@pytest.mark.parametrize("text,expected", [
    ("`a**b` c**", "<code>a**b</code> c**"),
    ("**x `y** z`", "**x <code>y** z</code>"),
    ("`**x**`", "<code>**x**</code>"),
    ("**a `c` b**", "<b>a <code>c</code> b</b>"),
])
def test_rich_never_misnests_tags(text, expected):
    assert rich(text) == expected


# --- Bot API calls ---------------------------------------------------------------------------------


def _bodies(route) -> list[dict]:
    return [json.loads(c.request.content) for c in route.calls]


def _route(method: str, result=None):
    return respx.post(f"{API}/botT/{method}").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": result if result is not None else True}))


@respx.mock
async def test_send_one_chunk_with_buttons_and_reply():
    route = _route("sendMessage", {"message_id": 11})
    tg = Telegram(token="T")
    ids = await tg.send(42, "hi", buttons=[[("Yes", "v1:w:notion")]], reply_to=5)
    (body,) = _bodies(route)
    assert ids == [11]
    assert body["reply_parameters"] == {"message_id": 5, "allow_sending_without_reply": True}
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "Yes", "callback_data": "v1:w:notion"}]]}
    await tg.aclose()


@respx.mock
async def test_send_two_chunks_threads_first_and_buttons_last():
    route = _route("sendMessage", {"message_id": 1})
    tg = Telegram(token="T")
    await tg.send(42, "x" * (MAX_LEN + 10), buttons=[[("A", "v1:n")]], reply_to=5)
    first, last = _bodies(route)
    assert "reply_parameters" in first and "reply_markup" not in first
    assert "reply_parameters" not in last and "reply_markup" in last
    await tg.aclose()


@respx.mock
async def test_send_with_raw_markup_for_force_reply():
    route = _route("sendMessage", {"message_id": 3})
    tg = Telegram(token="T")
    await tg.send(42, "Which service?", markup={"force_reply": True, "input_field_placeholder": "e.g. notion"})
    assert _bodies(route)[0]["reply_markup"]["force_reply"] is True
    await tg.aclose()


@respx.mock
async def test_new_methods_send_expected_payloads():
    routes = {m: _route(m) for m in (
        "answerCallbackQuery", "editMessageReplyMarkup", "setMessageReaction", "sendMessageDraft",
        "sendChatAction", "setMyCommands", "setMyDescription", "setMyShortDescription")}
    tg = Telegram(token="T")
    await tg.answer_callback("cb", "Done", alert=True)
    await tg.edit_buttons(42, 7, None)
    await tg.react(42, 7, "👀")
    await tg.draft(42, 99, "")
    await tg.typing(42)
    await tg.set_commands([("list", "Your watchlist")], {"type": "all_private_chats"})
    await tg.set_description("Long", "Short")
    body = lambda m: _bodies(routes[m])[0]
    assert body("answerCallbackQuery") == {"callback_query_id": "cb", "text": "Done", "show_alert": True}
    assert body("editMessageReplyMarkup") == {"chat_id": 42, "message_id": 7, "reply_markup": {"inline_keyboard": []}}
    assert body("setMessageReaction")["reaction"] == [{"type": "emoji", "emoji": "👀"}]
    assert body("sendMessageDraft") == {"chat_id": 42, "draft_id": 99, "text": ""}
    assert body("sendChatAction") == {"chat_id": 42, "action": "typing"}
    assert body("setMyCommands") == {"commands": [{"command": "list", "description": "Your watchlist"}],
                                     "scope": {"type": "all_private_chats"}}
    assert body("setMyDescription") == {"description": "Long"}
    assert body("setMyShortDescription") == {"short_description": "Short"}
    await tg.aclose()


@respx.mock
async def test_answer_callback_without_text_only_stops_the_spinner():
    route = _route("answerCallbackQuery")
    tg = Telegram(token="T")
    await tg.answer_callback("cb")
    assert _bodies(route) == [{"callback_query_id": "cb"}]
    await tg.aclose()


@respx.mock
async def test_webhook_asks_for_taps():
    route = _route("setWebhook")
    tg = Telegram(token="T")
    await tg.set_webhook("https://fp.test/telegram", "s")
    assert _bodies(route)[0]["allowed_updates"] == ["message", "callback_query"]
    await tg.aclose()


def test_split_packs_paragraphs_and_cuts_only_oversized_ones():
    from app.telegram import split

    assert split("a\n\nb", limit=10) == ["a\n\nb"]
    assert split("aaaa\n\nbbbb\n\ncccc", limit=10) == ["aaaa\n\nbbbb", "cccc"]
    assert split("x" * 25, limit=10) == ["x" * 10, "x" * 10, "x" * 5]
    assert split("") == []


@respx.mock
async def test_send_document_uploads_multipart_from_memory():
    route = _route("sendDocument", {"message_id": 77})
    tg = Telegram(token="T")
    sent = await tg.send_document(42, "redline.docx", b"PK\x03\x04docx-bytes", caption="Your <b>kit</b>", mime="application/x-test")
    await tg.aclose()
    assert sent == 77
    request = route.calls[0].request
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="chat_id"' in body and b"42" in body and b'filename="redline.docx"' in body
    assert b"PK\x03\x04docx-bytes" in body and b"application/x-test" in body and b'name="parse_mode"' in body


@respx.mock
async def test_send_document_failure_raises_without_the_token():
    respx.post(f"{API}/botT/sendDocument").mock(return_value=httpx.Response(200, json={"ok": False, "description": "too big"}))
    tg = Telegram(token="T")
    with pytest.raises(Exception, match="sendDocument failed: too big") as info:
        await tg.send_document(42, "x.pdf", b"%PDF")
    await tg.aclose()
    assert "botT" not in str(info.value)

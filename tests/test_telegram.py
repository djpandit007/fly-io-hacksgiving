from app.telegram import parse_update


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


def test_non_message_update_is_ignored():
    assert parse_update({"update_id": 1, "callback_query": {}}) is None


def test_rich_converts_light_markdown_safely():
    from app.telegram import rich

    out = rich("SIGN <now>\n- **Auto-renewal:** bills `monthly` & more")
    assert out == "SIGN &lt;now&gt;\n• <b>Auto-renewal:</b> bills <code>monthly</code> &amp; more"

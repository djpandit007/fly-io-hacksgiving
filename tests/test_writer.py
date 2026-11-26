import json

import httpx
import pytest
import respx

from app import writer
from app.config import settings
from app.gateway import Gateway

CHAT = "https://opencode.test/inference/openai/v1/chat/completions"


def reply(content, finish="stop", reasoning=10):
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"completion_tokens": reasoning + 5, "completion_tokens_details": {"reasoning_tokens": reasoning}},
    })


@respx.mock
async def test_empty_reply_is_retried_with_bigger_budget():
    route = respx.post(CHAT).mock(side_effect=[reply(None, "length", 1500), reply("SIGN — fine.")])
    async with Gateway(settings.opencode) as gw:
        assert await writer.chat(gw, "hi") == "SIGN — fine."
    budgets = [json.loads(c.request.content)["max_tokens"] for c in route.calls]
    assert budgets == list(writer.TOKEN_BUDGETS)


@respx.mock
async def test_persistently_empty_reply_raises_clear_error():
    respx.post(CHAT).mock(return_value=reply(None, "length", 4000))
    async with Gateway(settings.opencode) as gw:
        with pytest.raises(writer.EmptyReply, match="finish_reason=length"):
            await writer.chat(gw, "hi")


def sent(route):
    return json.loads(route.calls[0].request.content)


@respx.mock
async def test_summary_prompt_asks_for_structure_and_grounding():
    route = respx.post(CHAT).mock(return_value=reply("**What changed:** x"))
    async with Gateway(settings.opencode) as gw:
        await writer.summarise_change(gw, "S", "Fees", "old", "new")
    body = sent(route)
    system, user = body["messages"][0]["content"], body["messages"][1]["content"]
    assert "What changed" in user and "What it means for you" in user and "What you can do" in user
    assert "two sentences" not in user
    assert "never invent" in system.lower() and "**bold**" in system
    assert body["max_tokens"] == 4000


@respx.mock
async def test_watch_overview_sends_facts():
    route = respx.post(CHAT).mock(return_value=reply("Biggest deal: arbitration."))
    async with Gateway(settings.opencode) as gw:
        out = await writer.watch_overview(gw, "YouTube", "- 04 Mar 2026 · arbitration · Major")
    assert out == "Biggest deal: arbitration."
    user = sent(route)["messages"][1]["content"]
    assert "YouTube" in user and "04 Mar 2026" in user


def test_default_writer_model():
    from app.config import Settings
    assert Settings.model_fields["writer_model"].default == "deepseek-v4.1-flash"


def test_prompts_treat_given_text_as_data():
    for system in (writer.SYSTEM, writer.WRAPPED_SYSTEM):
        assert "never instructions" in system


def test_wrapped_prompt_has_formatting_rules():
    assert "**bold**" in writer.WRAPPED_SYSTEM and "No headings" in writer.WRAPPED_SYSTEM

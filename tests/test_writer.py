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

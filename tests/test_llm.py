"""Pydantic AI models whose HTTP requests go through the Gateway (signing, retries, metrics)."""

import json
import logging

import httpx
import pytest
import respx
from pydantic_ai import Agent, models
from pydantic_ai.exceptions import ModelHTTPError

from app.config import Provider, settings
from app.gateway import Gateway
from app.llm import GatewayTransport, model_for

CHAT = "https://opencode.test/inference/openai/v1/chat/completions"


def reply(text="hello", usage=None):
    body = {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


class FakeRecorder:
    def __init__(self):
        self.counts = []
        self.usages = []

    def count(self, kind, name, outcome="ok", ms=0):
        self.counts.append((kind, name, outcome, ms))

    def usage(self, provider, unit, amount):
        self.usages.append((provider, unit, amount))


async def run(gw, prompt="hi"):
    with models.override_allow_model_requests(True):
        return await Agent(model_for(gw, "m")).run(prompt)


@respx.mock
async def test_request_goes_through_gateway():
    route = respx.post(CHAT).mock(return_value=reply("hello"))
    gw = Gateway(settings.opencode)

    result = await run(gw)

    assert result.output == "hello"
    assert route.call_count == 1
    request = route.calls.last.request
    assert json.loads(request.content)["model"] == "m"
    assert request.headers["authorization"] == "Bearer oc-key"


@respx.mock
async def test_no_authorization_when_gateway_has_no_key():
    route = respx.post(CHAT).mock(return_value=reply())
    gw = Gateway(Provider(name="opencode", base_url="https://opencode.test/inference"))

    await run(gw)

    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_counts_and_usage_reported():
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.002}
    respx.post(CHAT).mock(return_value=reply(usage=usage))
    rec = FakeRecorder()
    gw = Gateway(settings.opencode, recorder=rec)

    await run(gw)

    assert [c[:3] for c in rec.counts] == [("provider", "opencode", "ok")]
    assert ("opencode", "usd", 0.002) in rec.usages
    assert ("opencode", "input_tokens", 10.0) in rec.usages
    assert ("opencode", "output_tokens", 5.0) in rec.usages


@respx.mock
async def test_retries_come_from_gateway(monkeypatch):
    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.gateway.asyncio.sleep", no_sleep)
    route = respx.post(CHAT).mock(side_effect=[httpx.Response(503, json={"error": "busy"}), reply("after retry")])
    gw = Gateway(settings.opencode)

    result = await run(gw)

    assert result.output == "after retry"
    assert route.call_count == 2
    assert slept == [1.0]


@respx.mock
async def test_error_status_reaches_pydantic_ai():
    respx.post(CHAT).mock(return_value=httpx.Response(400, json={"error": {"message": "bad request"}}))
    gw = Gateway(settings.opencode)

    with pytest.raises(ModelHTTPError) as caught:
        await run(gw)

    assert caught.value.status_code == 400
    assert not isinstance(caught.value.__cause__, httpx.HTTPStatusError)


@respx.mock
async def test_error_body_is_not_logged(caplog):
    """A provider may echo the prompt (question, quotes) in an error body; only the status is logged."""
    caplog.set_level(logging.DEBUG)
    body = {"error": {"message": "bad", "failed_generation": "Question: zebra-question about my secret"}}
    respx.post(CHAT).mock(return_value=httpx.Response(400, json=body))
    gw = Gateway(settings.opencode)

    with pytest.raises(ModelHTTPError):
        await run(gw, "Question: zebra-question about my secret")

    assert "zebra-question" not in caplog.text
    assert "-> 400" in caplog.text


@respx.mock
async def test_usage_reporting_tolerates_bodies_without_usage():
    respx.post(CHAT).mock(return_value=reply("no usage"))
    rec = FakeRecorder()
    gw = Gateway(settings.opencode, recorder=rec)

    result = await run(gw)

    assert result.output == "no usage"
    assert rec.usages == []


@respx.mock
async def test_non_json_body_is_passed_on_without_reporting():
    respx.post(CHAT).mock(return_value=httpx.Response(200, text="not json"))
    rec = FakeRecorder()
    gw = Gateway(settings.opencode, recorder=rec)

    async with httpx.AsyncClient(transport=GatewayTransport(gw)) as client:
        resp = await client.post(CHAT, content=b"{}", headers={"content-type": "application/json"})

    assert resp.status_code == 200
    assert resp.text == "not json"
    assert rec.usages == []


def test_model_settings_have_defaults():
    assert settings.agent_model == "deepseek-v4.1-flash"
    assert settings.verifier_model == "glm-5.3-flash"

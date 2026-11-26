"""Gateway error logging: the body is logged unless the caller says it may carry member text."""

import logging

import httpx
import pytest
import respx

from app.config import Provider
from app.gateway import Gateway

URL = "https://svc.test/thing"


def _gw() -> Gateway:
    return Gateway(Provider(name="svc", base_url="https://svc.test"))


@respx.mock
async def test_error_body_is_logged_by_default(caplog):
    caplog.set_level(logging.DEBUG)
    respx.post(URL).mock(return_value=httpx.Response(400, json={"error": "echo zebra-body"}))
    with pytest.raises(httpx.HTTPStatusError):
        await _gw().post("thing", json={})
    assert "zebra-body" in caplog.text
    assert "400" in caplog.text


@respx.mock
async def test_error_body_is_not_logged_when_asked(caplog):
    caplog.set_level(logging.DEBUG)
    respx.post(URL).mock(return_value=httpx.Response(400, json={"error": "echo zebra-body"}))
    with pytest.raises(httpx.HTTPStatusError):
        await _gw().post("thing", json={}, log_body=False)
    assert "zebra-body" not in caplog.text
    assert "svc POST thing -> 400" in caplog.text


@respx.mock
async def test_get_passes_log_body_through(caplog):
    caplog.set_level(logging.DEBUG)
    respx.get(URL).mock(return_value=httpx.Response(404, text="zebra-body"))
    with pytest.raises(httpx.HTTPStatusError):
        await _gw().get("thing", log_body=False)
    assert "zebra-body" not in caplog.text
    assert "-> 404" in caplog.text

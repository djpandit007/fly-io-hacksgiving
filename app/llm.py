"""Pydantic AI chat models on OpenCode whose HTTP requests go through ``Gateway``.

The OpenAI SDK that Pydantic AI drives is given an HTTP client whose transport hands each request to
the gateway, so connector signing, retries on 429/5xx and the owner's call and cost metrics work
exactly as they do for every other provider call. The SDK's own retries are off and its placeholder
API key never leaves the process.
"""

from __future__ import annotations

import httpx
from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.gateway import Gateway
from app.metrics import dig

API_PREFIX = "openai/v1"
# The body is handed over already decoded and complete, so these no longer describe it.
_STALE_HEADERS = ("content-encoding", "content-length", "transfer-encoding")


class GatewayTransport(httpx.AsyncBaseTransport):
    """Sends the OpenAI SDK's requests through ``Gateway.request`` and reports usage per response."""

    def __init__(self, gw: Gateway):
        self.gw = gw

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        marker = f"/{API_PREFIX}/"
        url_path = request.url.path
        if marker not in url_path:
            raise ValueError(f"not an OpenAI-compatible path: {url_path}")
        path = f"{API_PREFIX}/{url_path.split(marker, 1)[1]}"
        headers = {"content-type": ct} if (ct := request.headers.get("content-type")) else {}
        kwargs = {"params": request.url.params} if request.url.params else {}
        try:
            resp = await self.gw.request(request.method, path, content=await request.aread(), headers=headers, **kwargs)
        except httpx.HTTPStatusError as exc:
            # Pydantic AI turns the status into its own model error; it needs the response, not an exception.
            resp = exc.response
        self._report(resp)
        out_headers = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in _STALE_HEADERS]
        return httpx.Response(resp.status_code, headers=out_headers, content=resp.content)

    def _report(self, resp: httpx.Response) -> None:
        try:
            data = resp.json()
        except ValueError:
            return
        self.gw.report("usd", dig(data, "usage", "cost"))
        self.gw.report("input_tokens", dig(data, "usage", "prompt_tokens"))
        self.gw.report("output_tokens", dig(data, "usage", "completion_tokens"))


def model_for(gw: Gateway, name: str) -> Model:
    """A Pydantic AI chat model called ``name`` on OpenCode, reached only through ``gw``."""
    client = AsyncOpenAI(
        base_url=f"{gw.provider.base_url}/{API_PREFIX}",
        api_key="via-gateway",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=GatewayTransport(gw)),
    )
    return OpenAIChatModel(name, provider=OpenAIProvider(openai_client=client))

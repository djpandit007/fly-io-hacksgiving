"""One thin HTTP client for every external provider.

On a Sprite, ``Provider.base_url`` is the connector gateway path and the gateway signs the request,
so no Authorization header is sent. Off a Sprite (local dev), ``Provider.key`` is sent as a bearer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Self

import httpx

from app.config import Provider

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


class ProviderNotConfigured(RuntimeError):
    pass


class Gateway:
    def __init__(self, provider: Provider, timeout: float = 60.0, transport: httpx.AsyncBaseTransport | None = None):
        if not provider.configured:
            raise ProviderNotConfigured(f"{provider.name}: set FP_{provider.name.upper()}_BASE")
        headers = dict(provider.extra_headers)
        if key := provider.key.get_secret_value():
            headers[provider.auth_header] = f"{provider.auth_prefix}{key}"
        self.provider = provider
        self.client = httpx.AsyncClient(
            base_url=provider.base_url + "/", headers=headers, timeout=timeout, transport=transport
        )

    async def request(self, method: str, path: str, *, retries: int = 3, **kwargs: Any) -> httpx.Response:
        delay = 1.0
        for attempt in range(retries + 1):
            resp = await self.client.request(method, path.lstrip("/"), **kwargs)
            if resp.status_code not in RETRY_STATUSES or attempt == retries:
                if resp.is_error:
                    log.warning("%s %s %s -> %s: %s", self.provider.name, method, path, resp.status_code, resp.text[:500])
                resp.raise_for_status()
                return resp
            wait = float(resp.headers.get("retry-after", delay))
            await asyncio.sleep(min(wait, 30))
            delay *= 2
        raise AssertionError("unreachable")

    async def get(self, path: str, **kwargs: Any) -> Any:
        return (await self.request("GET", path, **kwargs)).json()

    async def post(self, path: str, **kwargs: Any) -> Any:
        resp = await self.request("POST", path, **kwargs)
        return resp.json() if resp.content else None

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

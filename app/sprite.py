"""Hold the Sprite awake while background work runs (Sprites Tasks API over the local socket).

A Sprite pauses about 30s after the last request. Work started from a webhook (parsing a PDF,
judging twenty clauses) outlives the request, so it holds a task with a short expiry and refreshes
it every minute; if the process dies, the hold lapses on its own.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx

log = logging.getLogger(__name__)
SOCKET = Path("/.sprite/api.sock")


def on_sprite() -> bool:
    return SOCKET.exists()


@asynccontextmanager
async def stay_awake(name: str, expire: str = "5m", refresh_every: float = 60) -> AsyncIterator[None]:
    if not on_sprite():
        yield
        return
    transport = httpx.AsyncHTTPTransport(uds=str(SOCKET))
    async with httpx.AsyncClient(transport=transport, base_url="http://sprite", timeout=10) as client:

        async def hold() -> None:
            try:
                await client.put(f"/v1/tasks/{name}", json={"expire": expire})
            except httpx.HTTPError as exc:
                log.warning("task hold %s failed: %s", name, exc)

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(refresh_every)
                await hold()

        await hold()
        beat = asyncio.create_task(heartbeat())
        try:
            yield
        finally:
            beat.cancel()
            try:
                await client.delete(f"/v1/tasks/{name}")
            except httpx.HTTPError:
                pass

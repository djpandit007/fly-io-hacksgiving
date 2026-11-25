"""Hold the Sprite awake while background work runs (Sprites Tasks API over the local socket).

A Sprite pauses about 30s after the last request. Work started from a webhook (parsing a PDF,
judging twenty clauses) outlives the request, so:

1. the webhook handler takes a hold *before* it answers (``hold``), while the inbound request still
   keeps the Sprite active — taking it afterwards races the pause;
2. the background job keeps refreshing that hold and releases it when done (``stay_awake``).

Holds expire on their own (short expiry, refreshed every minute), so a crash cannot pin the Sprite.
Each job uses its own task name, so one job finishing never releases another's hold.
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


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=str(SOCKET)), base_url="http://sprite", timeout=10
    )


async def hold(name: str, expire: str = "5m") -> bool:
    """Create or refresh a task hold. Returns False (and logs) if it could not be taken."""
    if not on_sprite():
        return True
    for attempt in range(3):
        try:
            async with _client() as client:
                resp = await client.put(f"/v1/tasks/{name}", json={"expire": expire})
                resp.raise_for_status()
                return True
        except httpx.HTTPError as exc:
            log.warning("task hold %s failed (attempt %d): %s", name, attempt + 1, exc)
            await asyncio.sleep(0.5 * (attempt + 1))
    return False


async def release(name: str) -> None:
    if not on_sprite():
        return
    try:
        async with _client() as client:
            await client.delete(f"/v1/tasks/{name}")
    except httpx.HTTPError:
        pass  # it expires anyway


@asynccontextmanager
async def stay_awake(name: str, expire: str = "5m", refresh_every: float = 60) -> AsyncIterator[None]:
    await hold(name, expire)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(refresh_every)
            await hold(name, expire)

    beat = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        beat.cancel()
        await release(name)

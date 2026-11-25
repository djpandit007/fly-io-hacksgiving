"""Per-key asyncio locks: one job at a time per member, per service, and for the daily run.

Replaces a single global lock, so one member's minute-long backfill doesn't stall everyone else.
Entries are dropped when no task holds or waits on them.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator


class LockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = defaultdict(int)

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] += 1
        try:
            async with lock:
                yield
        finally:
            self._users[key] -= 1
            if self._users[key] == 0:
                self._locks.pop(key, None)
                self._users.pop(key, None)

    def __len__(self) -> int:
        return len(self._locks)


locks = LockRegistry()

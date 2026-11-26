"""Show a member that the bot is working on their message.

    async with progress.working(tg, chat, message_id) as w:
        result = await agent.check_url(cl, url, on_stage=w.stage)
    await tg.send(chat, result, reply_to=message_id)

On entry: a 👀 reaction on their message and a "Thinking..." draft. While working: the draft shows the
current stage and is re-sent before Telegram drops it (~30 s), with the elapsed time once it runs long.
If drafts are refused, Telegram's "typing…" status is repeated instead. On exit: 👌 (Telegram doesn't
offer ✅ as a reaction) or 😢 when the work raised. Send the result *after* the block, so no refresh can
redraw the draft after the reply.

Progress is cosmetic: every Telegram error here is logged and ignored, never raised.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

log = logging.getLogger(__name__)

SEEN, DONE, FAILED = "👀", "👌", "😢"
REFRESH_S = 20.0     # drafts last about 30 s; refresh well before that
TYPING_S = 4.0       # "typing…" lasts 5 s
LONG_S = 20.0        # after this long, show the elapsed time

Stage = Callable[[str], None]


def no_stage(_: str) -> None:
    """Default `on_stage` for callers without a progress display."""


class Progress(Protocol):
    async def react(self, chat_id: int, message_id: int, emoji: str) -> None: ...
    async def draft(self, chat_id: int, draft_id: int, text: str = "") -> None: ...
    async def typing(self, chat_id: int) -> None: ...


async def _quiet(call: Awaitable[None], what: str) -> bool:
    try:
        await call
        return True
    except Exception as exc:  # noqa: BLE001 — progress must never change the outcome
        log.info("progress %s failed: %s", what, type(exc).__name__)
        return False


class Working:
    def __init__(self, tg: Progress, chat_id: int, refresh: float, typing_every: float) -> None:
        self.tg, self.chat_id = tg, chat_id
        self.refresh, self.typing_every = refresh, typing_every
        self.draft_id = secrets.randbelow(2**31 - 2) + 1   # non-zero; same id animates the changes
        self.started = time.monotonic()
        self.current = ""
        self.drafts_ok = True
        self._pending: set[asyncio.Task] = set()

    def text(self) -> str:
        elapsed = int(time.monotonic() - self.started)
        if elapsed < LONG_S:
            return self.current
        return f"{self.current or 'Still working…'} · {elapsed}s"

    def stage(self, text: str) -> None:
        """Name the current step; the draft updates straight away."""
        self.current = text
        task = asyncio.get_running_loop().create_task(self.show())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def show(self) -> None:
        if self.drafts_ok:
            if await _quiet(self.tg.draft(self.chat_id, self.draft_id, self.text()), "draft"):
                return
            self.drafts_ok = False
        await _quiet(self.tg.typing(self.chat_id), "typing")

    async def keep_alive(self) -> None:
        while True:
            await asyncio.sleep(self.refresh if self.drafts_ok else self.typing_every)
            await self.show()

    async def stop(self) -> None:
        for task in list(self._pending):
            task.cancel()
        for task in list(self._pending):
            with contextlib.suppress(asyncio.CancelledError):
                await task


@contextlib.asynccontextmanager
async def working(
    tg: Progress, chat_id: int, message_id: int | None, *, refresh: float = REFRESH_S, typing_every: float = TYPING_S
) -> AsyncIterator[Working]:
    w = Working(tg, chat_id, refresh, typing_every)
    if message_id:
        await _quiet(tg.react(chat_id, message_id, SEEN), "reaction")
    await w.show()
    loop = asyncio.create_task(w.keep_alive())
    ok = False
    try:
        yield w
        ok = True
    finally:
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop
        await w.stop()
        if message_id:
            await _quiet(tg.react(chat_id, message_id, DONE if ok else FAILED), "reaction")

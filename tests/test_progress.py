"""Progress while working: reaction, draft with stages, typing fallback, and never changing the outcome."""

import asyncio

import pytest

from app import progress
from app.telegram import TelegramError
from tests.test_bot import FakeTelegram


class FlakyTelegram(FakeTelegram):
    def __init__(self, *, drafts=True, reactions=True):
        super().__init__()
        self.drafts, self.reactions = drafts, reactions

    async def draft(self, chat, draft_id, text=""):
        if not self.drafts:
            raise TelegramError("Telegram sendMessageDraft failed: Bad Request")
        await super().draft(chat, draft_id, text)

    async def react(self, chat, message_id, emoji):
        if not self.reactions:
            raise TelegramError("Telegram setMessageReaction failed: REACTION_INVALID")
        await super().react(chat, message_id, emoji)


async def test_success_reacts_drafts_stages_and_finishes():
    tg = FakeTelegram()
    async with progress.working(tg, 1, 7) as w:
        w.stage("Reading the page…")
        await asyncio.sleep(0)
    assert [c[3] for c in tg.called("react")] == ["👀", "👌"]
    drafts = [c[2] for c in tg.called("draft")]
    assert drafts[0] == "" and "Reading the page…" in drafts


async def test_draft_is_refreshed_while_work_runs():
    tg = FakeTelegram()
    async with progress.working(tg, 1, 7, refresh=0.01):
        await asyncio.sleep(0.05)
    assert len(tg.called("draft")) >= 3


async def test_refresh_stops_after_the_block():
    tg = FakeTelegram()
    async with progress.working(tg, 1, 7, refresh=0.01):
        await asyncio.sleep(0.02)
    after = len(tg.called("draft"))
    await asyncio.sleep(0.05)
    assert len(tg.called("draft")) == after


async def test_failure_reacts_sad_and_reraises():
    tg = FakeTelegram()
    with pytest.raises(RuntimeError):
        async with progress.working(tg, 1, 7):
            raise RuntimeError("boom")
    assert [c[3] for c in tg.called("react")] == ["👀", "😢"]


async def test_draft_error_falls_back_to_typing():
    tg = FlakyTelegram(drafts=False)
    async with progress.working(tg, 1, 7, refresh=0.01, typing_every=0.01) as w:
        w.stage("Reading…")
        await asyncio.sleep(0.05)
    assert len(tg.called("typing")) >= 2 and not tg.called("draft")


async def test_reaction_errors_do_not_change_the_result():
    tg = FlakyTelegram(reactions=False)
    async with progress.working(tg, 1, 7) as w:
        result = "card"
        w.stage("x")
    assert result == "card" and tg.called("draft")


async def test_no_message_id_means_no_reaction():
    tg = FakeTelegram()
    async with progress.working(tg, 1, None):
        pass
    assert not tg.called("react") and tg.called("draft")


async def test_long_work_shows_elapsed_time(monkeypatch):
    tg = FakeTelegram()
    async with progress.working(tg, 1, 7) as w:
        w.started -= 45
        w.stage("Reading the document…")
        await asyncio.sleep(0)
    assert any(c[2].startswith("Reading the document… · 4") and c[2].endswith("s") for c in tg.called("draft"))


class SlowTelegram(FakeTelegram):
    """Drafts take a while, so quick stages overlap with requests in flight."""

    def __init__(self):
        super().__init__()
        self.in_flight = self.most_in_flight = 0

    async def draft(self, chat, draft_id, text=""):
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        await super().draft(chat, draft_id, text)
        self.in_flight -= 1


async def test_quick_stages_send_one_at_a_time_and_end_on_the_newest():
    tg = SlowTelegram()
    async with progress.working(tg, 1, 7) as w:
        for stage in ("Finding the terms page…", "Collecting the changes that matter…", "Writing the overview…"):
            w.stage(stage)
        await asyncio.sleep(0.1)
    drafts = [c[2] for c in tg.called("draft")]
    assert tg.most_in_flight == 1
    assert drafts[-1] == "Writing the overview…"
    assert len(drafts) <= 3  # the empty placeholder, then coalesced stages: no duplicates per stage


async def test_drafts_failing_during_quick_stages_fall_back_once():
    tg = FlakyTelegram(drafts=False)
    async with progress.working(tg, 1, 7) as w:
        w.stage("a")
        w.stage("b")
        await asyncio.sleep(0.02)
    assert not tg.called("draft") and 1 <= len(tg.called("typing")) <= 2

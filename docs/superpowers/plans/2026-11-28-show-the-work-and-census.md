# Show the Work and a Census: Implementation Plan (index)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Fine Print's existing engine visible to Hacksgiving judges (on camera and in the bot), and grade about 80 popular services so benchmarks appear.

**Architecture:** One small foundation lands first (schema v8, the button grammar, settings and shared test fakes). Four lanes then build in parallel worktrees, each owning its own files and its own plan document. Everything new is additive: with the new settings unset, the bot behaves as it does today.

**Tech Stack:** Python 3.12, FastAPI, SQLite, Pydantic, pydantic-ai, httpx, respx and pytest (asyncio auto mode), ruff, uv; Pillow is added by Lane 2.

**Spec:** `docs/superpowers/specs/2026-11-28-show-the-work-and-census-design.md`. Every lane plan argues from it; read both.

## The plans

| Lane | Plan | Branch | Starts after |
|---|---|---|---|
| 0. Foundation | `2026-11-28-lane-0-foundation.md` | `show-the-work-and-census` | now |
| 1. Inspector | `2026-11-28-lane-1-inspector.md` | `lane-1-inspector` | Lane 0 |
| 2. Card and the archive fix | `2026-11-28-lane-2-card-and-engine-fixes.md` | `lane-2-card` | Lane 0 |
| 3. Census | `2026-11-28-lane-3-census.md` | `lane-3-census` | Lane 0 (its last task after Lane 2 merges) |
| 4. Guests and kit | `2026-11-28-lane-4-guests-and-kit.md` | `lane-4-guests-kit` | Lane 0 |

Four spec items moved lanes to balance the work: the archive name fix (spec 3.7) is in Lane 2; the
name fix (1.8), the closed API pages (1.7) and the send-it-yourself draft (1.6) are in Lane 4.

**No new connectors.** After these plans were first written, Dhananjay dropped the switch to the
TypeSafeAI connector and the Apify crawl. Jev stays on OpenCode exactly as it is (`cl.ai`), and the
census reads pages with `agent.page_text`. No task adds a provider, a setting for one, or a client.

**No demo company.** Dhananjay also dropped the fictional company whose terms page was to be
switched on camera. The video opens a real change a real company made in the past year (Lane 1
Task 3 makes that possible, so it is not optional). Dropped with it, because they only showed with a
page we control and all three change the daily watch: an owner `/check`, a provenance record on
snapshots, and evidence screenshots. Lane 1 now has four tasks and Lane 4 seven. Lane 0's
`snapshots.provenance` column and `ce` button data stay unused.

## How it is executed

- **Model:** every implementer and reviewer subagent runs on **Opus 5.5** (Dhananjay's instruction).
  Dispatch with `model: "opus"`. Do not use forked agents: a fork inherits the session's model.
- **Worktrees:** Lane 0 is built on `show-the-work-and-census`. Each other lane gets its own worktree
  and branch, created from that branch once Lane 0's commits are on it.
- **One pull request per lane** into `main`; `tests` and `lint` must be green (the `protect-main`
  ruleset requires them). Lane 0 merges first.
- **Order inside the lanes:** Lane 2 runs its Task 4 (the archive name fix) first, because YouTube's
  history depends on it. Lane 3's last task waits for Lane 2 to merge. Lane 4's Task 6 (README) runs
  after the other lanes have merged, and its Task 7 last of all.
- **Merge order after Lane 0:** whichever lane finishes first.
- **Expected conflicts** when rebasing a lane onto a newer `main`, all small and resolved by keeping
  both sides: in `app/bot.py` the menus (`MEMBER_MENU`, `OWNER_MENU`), the `HELP` and `OWNER_HELP`
  text, the `elif` chain in `handle`, the cases in `_dispatch`, and `_watch` (Lane 1 adds a button,
  Lane 3 a census line and the several-at-once offer, Lane 4 a limit check).

## What only Dhananjay can do

| When | Task |
|---|---|
| When Lane 2 Task 2 reports | Pick the card's look from the three rendered PNGs. Until then, and if there is no pick by Sunday noon IST, it is `docket`. The pick is one line: `card.DEFAULT_STYLE`. |
| Before recording | Name a real watched service for the showcase button (`FP_SHOWCASE_SERVICE=Spotify`), and choose the real change to open on camera. Watch YouTube only after the archive name fix is deployed: a service that already has history is not read again. |
| Sunday | Deploy with `scripts/setup_sprite.sh`; send `/census run`; spot-check ten graded services and `/census drop` any that are wrong. Run the spec's live checks ("Verification", third bullet). |
| Sunday night, then Monday by 11 AM | Record the safe take, then the final take; post; submit by 12:30 PM IST. |

No stretch items are planned. The `/ask` verifier on Ollama Cloud was the one left on the list; it
is only planned if Dhananjay asks for it.

## Global Constraints

- Deadline: Monday 30 Nov 2026, 1:29 PM IST. Last merge Monday 9:00 AM IST. A safe take of the video
  is recorded Sunday night with whatever is live.
- Commit with explicit paths. Never `git add -A` or `git add .`. Leave untracked files you did not
  create alone: other agents work in this repository.
- Run before every commit: `uv run pytest -q` and `uvx ruff@0.17.6 check .`. Both must pass.
- No web UI. No simulated, replayed or reset path in the product. No storing members' documents.
- Sending from the app (`advocate.approve`, receipts, its limits) is not changed by any task.
- No changes to thresholds, prompts, the worker runners (`workers/`) or the checks in
  `app/discover.py` (`USER_CONTENT`, `is_user_content`).
- New button actions never join `taps.SLOW` unless the task says so: a slow tap strips every button
  from its message before it runs.
- Telegram: button data ≤ 64 bytes, messages ≤ 4096 characters, captions ≤ 1024.
- Everything shown to a member is HTML-escaped with `app.telegram.esc` unless it is fixed copy.
- Every new tap that names a change or a service checks that the tapping member watches it; if not,
  answer `bot.STALE_TAP` ("That button no longer works.") and send nothing.
- Metrics and `swarm_events` carry labels and durations only: never a URL, a name a member typed,
  or document text.
- Docs and copy never claim a 300 ms restore, forked Sprites or HMAC-signed tokens. The measured
  restore is about 15 s warm and 48 s cold.
- Test style: tests are plain `async def` (pytest-asyncio auto mode); HTTP is faked with `respx`;
  providers are reached through a real `agent.Clients()` using the environment in
  `tests/conftest.py`; Telegram is `tests.test_bot.FakeTelegram`.

## Review Focus

The inputs and conditions the spec implies but does not spell out, most likely first. Each has a
pinned test in the task that owns the code.

1. **A tap arrives after a restart or a Sprite sleep.** RAM-only offers are gone. Expected: a plain
   "expired" answer, never an exception. (Lane 3 Task 7)
2. **A census page that is not the terms:** a cookie wall, a login page, a 404 served with status
   200. Expected: not graded, listed as failed, nothing published. (Lane 3 Task 5)
3. **Text that does not fit or is not safe:** a diff longer than a message, a heading or service
   name containing `<` or `&`. Expected: cut at a boundary with tags balanced, everything escaped.
   (Lane 1 Task 2, Lane 3 Task 6)
4. **Two things at once:** `/census run` while one is running; a double tap on "Yes, watch all".
   Expected: one run, and a plain answer for the second. (Lane 3 Tasks 5 and 7)
5. **A member with nothing yet:** `/wrapped` with an empty watchlist, `/radar` before any census,
   the showcase button when the named service has no change. Expected: one helpful sentence, not a
   card of zeros or a crash. (Lane 2 Task 3, Lane 3 Task 6, Lane 4 Task 4)

## What gets cut first

From the spec, in this order: the Wrapped comparison line (Lane 3 Task 8) and `/radar <service>`;
the send-it-yourself draft (Lane 4 Task 7); watching several at once (Lane 3 Task 7); the showcase
button (Lane 4 Task 4). Never cut: the inspector buttons and opening a past change (Lane 1 Tasks 2
and 3), the narrated steps, the archive name fix (Lane 2 Task 4), the card, the census and `/radar`,
the script, README and licence, the closed API pages, and the limits.

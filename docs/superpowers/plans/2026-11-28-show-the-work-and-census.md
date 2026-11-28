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
| 2. Card and two engine fixes | `2026-11-28-lane-2-card-and-engine-fixes.md` | `lane-2-card` | Lane 0 |
| 3. Census | `2026-11-28-lane-3-census.md` | `lane-3-census` | Lane 0 (its last task after Lane 2 merges) |
| 4. Guests and kit | `2026-11-28-lane-4-guests-and-kit.md` | `lane-4-guests-kit` | Lane 0 |

Two spec items moved lanes to balance the work: the TypeSafeAI switch (spec 3.7) and the archive name
fix (3.8) are in Lane 2; the name fix (1.8), the closed API pages (1.7) and the send-it-yourself
draft (1.6) are in Lane 4, next to the demo company that depends on them.

## How it is executed

- **Model:** every implementer and reviewer subagent runs on **Opus 5.5** (Dhananjay's instruction).
  Dispatch with `model: "opus"`. Do not use forked agents: a fork inherits the session's model.
- **Worktrees:** Lane 0 is built on `show-the-work-and-census`. Each other lane gets its own worktree
  and branch, created from that branch once Lane 0's commits are on it.
- **One pull request per lane** into `main`; `tests` and `lint` must be green (the `protect-main`
  ruleset requires them). Lane 0 merges first.
- **Merge order after Lane 0:** Lane 4 as soon as its first four tasks are done (the demo company
  depends on the name fix), then whichever lane finishes; Lane 3's last task after Lane 2.
- **Expected conflicts** when rebasing a lane onto a newer `main`, all small and resolved by keeping
  both sides: in `app/bot.py` the menus (`MEMBER_MENU`, `OWNER_MENU`), the `HELP` and `OWNER_HELP`
  text, the `elif` chain in `handle`, the cases in `_dispatch`, and `_watch` (Lane 1 adds a button,
  Lane 3 a census line and the several-at-once offer, Lane 4 a limit check).

## What only Dhananjay can do

| When | Task |
|---|---|
| Saturday | Create the TypeSafeAI and Apify connectors in the Fly dashboard (name prefix `fly-io-hacksgiving`, label `fineprint`) and put their gateway bases in `.env.sprite` as `FP_TYPESAFE_BASE` and `FP_APIFY_BASE`. Either can be skipped: Jev then stays on OpenCode, and the census reads pages itself. |
| When Lane 2 Task 2 reports | Pick the card's look from the three rendered PNGs. Until then, and if there is no pick by Sunday noon IST, it is `docket`. The pick is one line: `card.DEFAULT_STYLE`. |
| When Lane 4 Task 6 merges | Put `demo/site` on a free static host under a subdomain of his choice, following `demo/README.md`; `/watch` its terms page; set `FP_SHOWCASE_SERVICE`. |
| Sunday | Deploy with `scripts/setup_sprite.sh`; send `/census run`; spot-check ten graded services and `/census drop` any that are wrong. Run the spec's live checks ("Verification", third bullet). |
| Sunday night, then Monday by 11 AM | Record the safe take, then the final take; post; submit by 12:30 PM IST. |

The stretch items (the `/ask` verifier on Ollama Cloud, Box as an evidence locker) have no plan yet.
They are planned on Sunday evening only if everything above is live.

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

1. **A tap arrives after a restart or a Sprite sleep.** RAM-only offers are gone and a stored
   screenshot's file may be missing. Expected: a plain "expired" or "no longer available" answer,
   never an exception. (Lane 1 Task 6, Lane 3 Task 8)
2. **A census page that is not the terms:** a cookie wall, a login page, a 404 served with status
   200. Expected: not graded, listed as failed, nothing published. (Lane 3 Task 5)
3. **Text that does not fit or is not safe:** a diff longer than a message, a heading or service
   name containing `<` or `&`. Expected: cut at a boundary with tags balanced, everything escaped.
   (Lane 1 Task 6, Lane 3 Task 6)
4. **Two things at once:** `/census run` while one is running; a double tap on "Yes, watch all";
   `/check` on a name nobody watches. Expected: one run, and a plain answer for the second.
   (Lane 1 Task 2, Lane 3 Tasks 5 and 8)
5. **A member with nothing yet:** `/wrapped` with an empty watchlist, `/radar` before any census,
   the showcase button when the demo service has no change. Expected: one helpful sentence, not a
   card of zeros or a crash. (Lane 2 Task 3, Lane 3 Task 6, Lane 4 Task 5)

## What gets cut first

From the spec, in this order: stretch items; the Wrapped comparison line (Lane 3 Task 9) and
`/radar <service>`; the archive name fix (Lane 2 Task 5); watching several at once (Lane 3 Task 8);
Apify (Lane 3 Task 7); the TypeSafeAI switch (Lane 2 Task 4); the showcase button (Lane 4 Task 5);
the open-a-change list (Lane 1 Task 7). Never cut: `/check`, the inspector buttons, the narrated
steps, the send-it-yourself draft, the name fix, the card, the demo company, the script, README and
licence, the closed API pages, and the limits.

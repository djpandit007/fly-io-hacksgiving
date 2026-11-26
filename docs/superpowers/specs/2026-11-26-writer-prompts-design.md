# Better writer prompts, readable scores, cheaper model, one /watch reply

## Context
The chat model's prompts (`app/writer.py`) are minimal, and what members see has four problems:
1. Red-flag percentages (`🔴 auto renews (83%)`, `app/agent.py:444`) read as "how bad" but mean "how sure the judge is the clause exists". `severity 1.7/3` (`agent.py:90`) is just as opaque.
2. Summaries are capped at "at most two sentences" (`writer.py:91`), and the thinking budget is tight (`TOKEN_BUDGETS = (1500, 4000)`). Spending more thinking tokens is fine if it buys fuller summaries.
3. `deepseek-v4.1-flash` is 60% off on OpenCode Zen ($0.12/1M input). Zen docs say providers keep no data and don't train on it, with only OpenAI/Anthropic/Mistral/xAI listed as exceptions, so this keeps the zero-retention reason `glm-5.3-flash` was picked for.
4. `/watch` sends one Telegram message per past change (`app/bot.py:100`).

Decisions made with the user: `/watch` becomes **one reply: a model-written overview followed by the existing cards stitched together**. Daily-check alerts stay one message per change. Scores show **words plus the number**. Per-change summaries use a **short structure** (what changed / what it means / what you can do).

## Changes

### Scores in words (`app/judge.py`, `app/agent.py`)
- `judge.py`: add `SEVERITY_LABELS = ["Cosmetic", "Minor", "Worth knowing", "Major"]` next to `SEVERITY_LEVELS` (the same four levels), plus `severity_label(score) -> str` using `round()` and clamping.
- `ChangeReport.to_html` badge: `data · Major (2.6/3) · favours the company` (favours: company → "favours the company", consumer → "favours you", neutral → "neutral").
- `verdict_card` flags: add a "What I checked for:" line, then `🔴 Found (83% sure): auto renews` / `🟡 Unclear (45% sure): …` / `🟢 Not found (92% sure): …`. Keep the 0.7/0.3 cut-offs. For 🟢 the number is `1-p`, so it always backs up the word (my call).
- Wrapped facts (`agent.py:546`) also pass the label to the model: `worst severity Major (2.6/3)`.

### Prompts and thinking (`app/writer.py`)
- Rewrite `SYSTEM`: the reader is one ordinary member, not a lawyer. Ground every claim in the given text and never invent deadlines, addresses or amounts. Say plainly when the text is ambiguous. No legal advice. Output formatting must be light Markdown that `telegram.rich()` handles: `**bold**` and `-` bullets only, with no headings, tables or links.
- Allow thinking: tell the model to work out what actually changed before writing. Change `TOKEN_BUDGETS` to `(4000, 12000)` and keep the existing empty-reply retry.
- `summarise_change` prompt: up to ~4 short sentences as three labelled lines: **What changed**, **What it means for you**, **What you can do** (that last line only when the text gives a way to act, with the how and the deadline).
- `verdict_card` prompt: unchanged in shape. It gets the new SYSTEM.
- New `watch_overview(gw, service, items: str) -> str`: input is the stored per-change facts (date, heading, category, severity label, favours, summary, deadline), which is bounded and cheap. Output: 2–4 sentences on what matters most this year, then the opt-out deadlines still open. Under ~120 words.

### Model (`app/config.py`, `.env.example`, `README.md`)
- `writer_model` default becomes `deepseek-v4.1-flash` (the paid ID, not `-free`). Update the `.env.example` comment and the README table row, citing zero data retention on Zen.
- Before merging, re-check https://opencode.ai/docs/zen for price and retention, and make one live call to confirm the model ID works at `openai/v1/chat/completions`.

### One /watch reply (`app/agent.py`, `app/bot.py`, `app/intake.py`)
- New `agent.watch_messages(cl, svc, reports) -> list[str]`: a header `<b>{svc}</b> — past year ({n} changes)`, then the overview (`rich()`; skip it silently if the writer fails or AI is off), then each `report.to_html()` separated by blank lines, **packed into messages ≤ `telegram.MAX_LEN` at card boundaries**. This matters because `Telegram.send` splits by raw characters and could cut an HTML tag in half. A single card longer than the limit falls back to `send`'s own split.
- `bot._watch`: send `watch_messages(...)` instead of the loop. The "Watching X…" and "no material changes" messages stay.
- `intake.py:153` (email `/watch` equivalent): already one email; put the same overview at the top of `cards` for consistency. My call.

## Tests
- `tests/test_agent.py:238`: update to the new flag format and add a severity-badge assertion.
- `tests/test_writer.py`: budgets test still passes via `TOKEN_BUDGETS`. Add a `watch_overview` prompt test in the existing style.
- New: `watch_messages` packs many cards into ≤ MAX_LEN chunks without splitting a card, puts the overview first, and survives an overview failure. `bot._watch` sends one message for a few changes.
- `tests/test_judge.py`: `severity_label` edges (0, 1.49, 1.5, 3).

## Verification
- `uv run pytest` and `uv run ruff check .`
- Live: with `FP_OPENCODE_KEY` set locally, `/watch` a service with history (e.g. via `scripts/telegram_poll.py`). Check that one reply arrives (or a few, at card boundaries), the overview reads well, badges show words + numbers, and logs show no `writer returned no text` warnings at the new budgets.

## Approaches considered
- Chosen: prompt + config + render changes inside existing code paths; thinking via larger `max_tokens` budgets.
- Rejected: passing a reasoning-effort API parameter (unconfirmed support on OpenCode for DeepSeek).
- /watch shape: user picked stitched cards with a model-written overview on top, over a compact list or cards alone.

## Next
Invoke `superpowers:writing-plans`.

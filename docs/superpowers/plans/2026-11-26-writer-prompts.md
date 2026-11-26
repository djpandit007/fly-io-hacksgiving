# Writer Prompts, Readable Scores, One /watch Reply: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fuller, grounded writer output on a cheaper model, scores members can read, and `/watch` answered in one reply (overview + stitched cards).

**Architecture:** Prompt and budget changes stay in `app/writer.py`. Label helpers go in `app/judge.py`, next to the levels they name. Rendering and message packing go in `app/agent.py`, which `app/bot.py` and `app/intake.py` call. The pipeline and storage are unchanged.

**Tech Stack:** Python 3.12, pydantic, httpx, respx + pytest (async tests), ruff, uv.

**Spec:** `docs/superpowers/specs/2026-11-26-writer-prompts-design.md`

## Global Constraints

- Writer model default: `deepseek-v4.1-flash` (paid ID, not `-free`).
- `TOKEN_BUDGETS = (4000, 12000)`.
- Severity labels, index = rounded score clamped to 0..3: `["Cosmetic", "Minor", "Worth knowing", "Major"]`.
- Flag cut-offs are unchanged: 🔴 `p >= 0.7` "Found", 🟡 `p >= 0.3` "Unclear", 🟢 otherwise "Not found". The number shown is `p` for 🔴/🟡 and `1-p` for 🟢, formatted `({x:.0%} sure)`.
- Telegram message limit: `app.telegram.MAX_LEN` (4000).
- Writer output may use only `**bold**` and `-` bullets (what `telegram.rich()` renders).
- Daily-check deliveries (`agent.tick`, `notify.deliver`) are **not** changed.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. The overview writer fails or AI is unconfigured: `/watch` still sends the header and cards, with no error text. *(Task 3 test)*
2. One card is longer than `MAX_LEN`: it goes in its own message and is never merged with others. *(Task 3 test)*
3. The year has no material changes: no overview call is made, and the existing "No material changes…" message stays. *(Task 3 test)*
4. A report with `verdict=None` (judge down) still renders the "unjudged" badge and still feeds the overview without crashing. *(Task 1 and Task 3 tests)*
5. The overview text contains `<` or `&`: it is escaped through `rich()`. *(Task 3 test)*

---

### Task 1: Scores in words

**Files:**
- Modify: `app/judge.py` (near `SEVERITY_LEVELS`, line 35)
- Modify: `app/agent.py` (`ChangeReport.to_html` line 88–103; `verdict_card` flag lines ~442–445; `wrapped` facts line 546)
- Test: `tests/test_judge.py`, `tests/test_agent.py`

**Interfaces:**
- Produces: `judge.SEVERITY_LABELS: list[str]`, `judge.severity_label(score: float) -> str`, `agent.flag_line(key: str, p: float) -> str`, `agent.FAVOURS_TEXT: dict[str, str]` = `{"company": "favours the company", "consumer": "favours you", "neutral": "neutral"}`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_judge.py
@pytest.mark.parametrize("score,label", [(0, "Cosmetic"), (1.49, "Minor"), (1.5, "Worth knowing"), (2.6, "Major"), (3, "Major"), (3.4, "Major")])
def test_severity_label(score, label):
    assert judge.severity_label(score) == label

# tests/test_agent.py
def test_flag_lines_say_what_the_number_means():
    assert agent.flag_line("auto_renews", 0.83) == "🔴 Found (83% sure): auto renews"
    assert agent.flag_line("data_sharing", 0.45) == "🟡 Unclear (45% sure): data sharing"
    assert agent.flag_line("hard_to_cancel", 0.08) == "🟢 Not found (92% sure): hard to cancel"

def test_change_badge_uses_words_and_number():
    v = Verdict(material=0.9, category="data", opt_out=0.1, favours="company", severity=2.6)
    r = agent.ChangeReport(change_id=1, service="S", heading="H", kind="modified",
                           effective_at=datetime(2026, 3, 4, tzinfo=UTC), verdict=v, summary="x", redline="x")
    assert "data · Major (2.6/3) · favours the company" in r.to_html()
    assert "unjudged" in r.model_copy(update={"verdict": None}).to_html()
```
Also update `test_verdict_card_survives_writer_failure` so it asserts `"🔴 Found (90% sure): arbitration" in card` and `"What I checked for:" in card`.

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_judge.py tests/test_agent.py -k "severity_label or flag_line or badge or survives_writer" -v`
Expected: FAIL (`severity_label` / `flag_line` are not defined, and the old format is still in the card).

- [ ] **Step 3: Implement**

`severity_label` uses `SEVERITY_LABELS[min(max(round(score), 0), 3)]`. Note that Python's `round(1.5) == 2`, which the test expects. Badge: `f"{v.category} · {severity_label(v.severity)} ({v.severity:.1f}/3) · {FAVOURS_TEXT[v.favours]}"`. `verdict_card` emits `"What I checked for:\n" + "\n".join(flag_line(k, p) ...)`. In `wrapped`, the facts line becomes `worst severity {severity_label(w)} ({w:.1f}/3)`.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass. Fix any other assertions on the old `(90%)` or `severity x/3` text, e.g. in `tests/test_mail.py`.

- [ ] **Step 5: Commit**: `git commit -am "Show scores as words plus the number"` (with the Co-Authored-By line)

---

### Task 2: Writer prompts, thinking budget, model

**Files:**
- Modify: `app/writer.py`, `app/config.py:58`, `.env.example` (`FP_WRITER_MODEL` line), `README.md:45`
- Test: `tests/test_writer.py`

**Interfaces:**
- Produces: `writer.watch_overview(gw: Gateway, service: str, facts: str) -> str`

- [ ] **Step 1: Write failing tests**

```python
def sent(route):
    return json.loads(route.calls[0].request.content)

@respx.mock
async def test_summary_prompt_asks_for_structure_and_grounding():
    route = respx.post(CHAT).mock(return_value=reply("**What changed:** x"))
    async with Gateway(settings.opencode) as gw:
        await writer.summarise_change(gw, "S", "Fees", "old", "new")
    body = sent(route)
    user, system = body["messages"][1]["content"], body["messages"][0]["content"]
    assert "What changed" in user and "What it means for you" in user and "What you can do" in user
    assert "two sentences" not in user
    assert "never invent" in system.lower() and "**bold**" in system
    assert body["max_tokens"] == 4000

@respx.mock
async def test_watch_overview_sends_facts():
    route = respx.post(CHAT).mock(return_value=reply("Biggest deal: arbitration."))
    async with Gateway(settings.opencode) as gw:
        out = await writer.watch_overview(gw, "YouTube", "- 04 Mar 2026 · arbitration · Major …")
    assert out == "Biggest deal: arbitration."
    assert "YouTube" in sent(route)["messages"][1]["content"] and "04 Mar 2026" in sent(route)["messages"][1]["content"]

def test_default_writer_model():
    from app.config import Settings
    assert Settings.model_fields["writer_model"].default == "deepseek-v4.1-flash"
```

- [ ] **Step 2: Run them and confirm they fail**: `uv run pytest tests/test_writer.py -v`. Expected: the three new tests FAIL.

- [ ] **Step 3: Implement** to the spec's "Prompts and thinking" section:
  - `SYSTEM`: the reader is an ordinary member, not a lawyer. Think through what actually changed before writing. Ground every claim in the given text, never invent deadlines, addresses or amounts, and say when the text is ambiguous. No legal advice. Formatting: `**bold**` and `-` bullets only, with no headings, tables or links.
  - `TOKEN_BUDGETS = (4000, 12000)`.
  - `summarise_change`: three labelled lines, **What changed** / **What it means for you** / **What you can do**. Include the last line only when the text gives a way to act, with the how and by when. About 4 short sentences in total.
  - `watch_overview`: the user prompt names the service and passes the facts. It asks for 2–4 sentences on what matters most this year, then any opt-out deadlines still open, all under ~120 words.
  - Model default goes in `config.py`, `.env.example` (comment: "60% off on OpenCode Zen; zero data retention per Zen docs") and the README row.

- [ ] **Step 4: Run them and confirm they pass**: `uv run pytest -q && uv run ruff check .`. Expected: all pass.

- [ ] **Step 5: Commit**: `"Richer writer prompts, larger thinking budget, DeepSeek V4.1 Flash writer"`

---

### Task 3: One /watch reply

**Files:**
- Modify: `app/agent.py` (new functions after `subscribe`), `app/bot.py:96-101`, `app/intake.py:153-166`
- Test: `tests/test_agent.py`, `tests/test_bot.py`

**Interfaces:**
- Consumes: `writer.watch_overview` (Task 2), `judge.severity_label`, `agent.FAVOURS_TEXT` (Task 1)
- Produces:
  - `agent.pack(parts: list[str], limit: int = MAX_LEN, sep: str = "\n\n") -> list[str]`: greedy and order-preserving. It starts a new message when adding the next part plus `sep` would go over `limit`. A part longer than `limit` gets a message of its own.
  - `async agent.overview(cl: Clients, svc: Service, reports: list[ChangeReport]) -> str`: the Telegram-HTML overview (`rich()` applied), or `""` when `cl.ai` is None, `reports` is empty or the writer raises. The facts are one line per report: `- {effective_at:%d %b %Y} · {heading} · {category|unjudged} · {severity label|—} · {favours text|—} · {summary}` plus `· opt-out by {deadline:%d %b %Y}` when set.
  - `async agent.watch_messages(cl: Clients, svc: Service, reports: list[ChangeReport]) -> list[str]` = `pack([header, overview?, *cards])` with header `f"<b>{esc(svc.name)}</b> — past year ({n} change{'s' if n != 1 else ''})"`

- [ ] **Step 1: Write failing tests**

```python
def test_pack_keeps_cards_whole():
    cards = ["a" * 1500] * 5
    out = agent.pack(cards, limit=4000)
    assert len(out) == 3 and all(len(m) <= 4000 for m in out) and "".join(out).count("a") == 7500

def test_pack_gives_an_oversized_card_its_own_message():
    out = agent.pack(["x", "y" * 5000, "z"], limit=4000)
    assert out == ["x", "y" * 5000, "z"]

async def test_watch_messages_overview_first(...)          # mock CHAT -> "Arbitration <now> & more"; 2 reports
    # assert len(msgs) == 1; msgs[0] starts with header; "Arbitration &lt;now&gt; &amp; more" appears before first card text
async def test_watch_messages_without_overview_when_writer_fails(...)   # CHAT returns content None twice
    # assert header and both cards present, no "went wrong"/error text
async def test_overview_tolerates_unjudged_reports(...)    # report with verdict=None -> facts line contains "unjudged"
```
In `tests/test_bot.py`, using `FakeTelegram`, `respx` and `monkeypatch` of `agent.subscribe` to return 3 reports, add `test_watch_sends_one_reply`. It asserts that exactly 2 messages go to the chat: "Watching …" and one combined reply containing all 3 card headings. Add `test_watch_with_no_changes_skips_overview`: no CHAT call, and the message "No material changes" is sent.

- [ ] **Step 2: Run them and confirm they fail**: `uv run pytest tests/test_agent.py tests/test_bot.py -k "pack or watch" -v`. Expected: FAIL (the names are not defined, and `bot` still sends 3 card messages).

- [ ] **Step 3: Implement** `pack`, `overview` and `watch_messages` in `agent.py`. In `bot._watch`, replace the loop with `for m in await agent.watch_messages(cl, svc, reports): await tg.send(chat, m)`. In `intake._terms_update`'s `else` branch, when `past` is non-empty, insert `await agent.overview(cl, svc, past)` (if non-empty) before the last-3 cards.

- [ ] **Step 4: Run them and confirm they pass**: `uv run pytest -q && uv run ruff check .`. Expected: all pass.

- [ ] **Step 5: Commit**: `"Send /watch as one reply: overview plus stitched cards"`

---

### Task 4: Live check

- [ ] **Step 1:** Re-read https://opencode.ai/docs/zen and confirm `deepseek-v4.1-flash` is listed, its price is discounted, and it isn't in the retention exceptions. If any of that has changed, stop and tell the user.
- [ ] **Step 2:** With `FP_OPENCODE_KEY` set, run `uv run python -c "import asyncio; from app import writer; from app.gateway import Gateway; from app.config import settings; print(asyncio.run((lambda: (lambda gw: writer.summarise_change(gw,'Test','Fees','Fee is $5.','Fee is $9; opt out within 30 days by emailing optout@x.com.'))(Gateway(settings.opencode)))()))"`. Expected: three labelled lines that mention $9 and the 30 days, and no `writer returned no text` warning.
- [ ] **Step 3:** `/watch` a service with history via `scripts/telegram_poll.py`. Expected: "Watching…" and then one reply (or a few, split at card boundaries) with the overview on top and badges like `data · Major (2.6/3) · favours the company`.

# Lane 1: Inspector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any watcher open a real change from the past year and see its diff and how it was judged, and have the Sprites narrate their own steps wherever a member waits on them.

**Architecture:** The Swarm reports each step of a worker job through the existing progress callback, which is threaded from the bot's progress bubble down to the page reader. A new `app/inspector.py` renders stored facts only (no provider calls) behind access-checked buttons. Nothing in the daily run (`tick`, `check_bundle`, `record_version`) changes.

**Tech Stack:** Python 3.12, SQLite, Pydantic, httpx, respx, pytest (asyncio auto mode).

**Spec:** `docs/superpowers/specs/2026-11-28-show-the-work-and-census-design.md`, sections 1.2 and 1.5.

## Global Constraints

- All of `docs/superpowers/plans/2026-11-28-show-the-work-and-census.md` "Global Constraints" apply.
- Lane 0 is already on your branch: the button data `cd`, `ch`, `co`, `cl`, and `FakeTelegram.send_document` and `watch()` in `tests/test_bot.py`.
- Do not change `agent.tick`, `agent.check_bundle`, `agent.record_version` or `app/intake.py`. The daily watch must behave exactly as it does today.
- Do not touch `advocate.approve`, `advocate.finish`, `_printable` or `bot._name_for` (Lane 4 owns them).
- Narration is cosmetic and best effort: it never raises and never changes what a job returns.
- `app/agent.py` must not import `app/inspector.py` (the inspector imports the agent).
- Not built in this lane (dropped with the demo company on 28 Nov): an owner `/check` command, a provenance record on snapshots, and evidence screenshots for changes. The `snapshots.provenance` column and the `ce` button data from Lane 0 stay unused.

## Review Focus

1. **Unsafe or oversized diff text:** `<`, `&` and a 30,000-word section. Expected: escaped, cut at a word, tags balanced. (Task 2)
2. **A non-watcher taps a forged id** for another member's service. Expected: `bot.STALE_TAP`, nothing sent. (Tasks 2 and 3)
3. **A narration callback that raises, or a job that fails.** Expected: the job's outcome is unchanged, and the rewind is still announced. (Task 1)
4. **A change with no earlier version, or one that was never judged.** Expected: the lines that do not apply are left out; no crash. (Task 2)
5. **An archived page whose URL has `&` or a quote in it.** Expected: the link's address is escaped. (Task 2)

---

### Task 1: The Sprites narrate their steps

**Files:**
- Modify: `app/swarm.py` (`job`, `run_job`, `scout`; new `lock_stage`, `restore_stage`, `RUN_STAGES`), `app/agent.py` (`scout_text`, `page_text`, `check_url`, `_onboard_document`, `onboard_service`, `subscribe`)
- Test: `tests/test_swarm.py`, `tests/test_agent.py`

**Interfaces:**
- Produces:
  - `Swarm.job(role, allow_hosts, *, on_stage: Stage = no_stage)`, `Swarm.run_job(..., on_stage: Stage = no_stage)`, `Swarm.scout(url, *, screenshot=False, extra_hosts=(), on_stage: Stage = no_stage)`. `Stage` and `no_stage` come from `app.progress`.
  - `swarm.lock_stage(role: str, allow_hosts: Sequence[str]) -> str` and `swarm.restore_stage(role: str) -> str`.
  - `agent.scout_text(cl, url, public=None, links=None, *, on_stage: Stage = no_stage)` and `agent.page_text(cl, url, *, public=None, may_scout=None, links=None, on_stage: Stage = no_stage)`, which pass it to `Swarm.scout`.
  - `agent.check_url` passes its `on_stage` to `page_text`. `agent.onboard_service(conn, cl, svc, on_stage: Stage = no_stage)` and `_onboard_document` pass it on; `agent.subscribe` passes its own. `check_bundle` is not changed.

Exact stage texts:

| When | Text |
|---|---|
| lock, hosts given | `🔒 Locking the Scout's network to acme.test…` (the first host) |
| lock, hosts equal `IDLE_HOSTS[role]` and not empty | `🔒 Locking the Advocate's network to its connectors only…` |
| lock, empty list | `🔒 Closing the Advocate's network…` |
| run, by `detail` | `playwright_crawl`: `🌐 Reading the page in a quarantined browser…`; `counterparty_counsel`: `⚖️ The other side's counsel is reading the contract…`; `consumer_counsel`: `🛡 Your counsel is drafting the changes…`; `redline_build`: `📝 Building the tracked-changes document…`; anything else: `⚙️ Working on the Advocate…` |
| restore | `⏪ Rewinding the Scout to its clean checkpoint…` |

No lock stage when `allow_hosts is None`. The restore stage is announced before the restore starts, including when the job failed.

- [ ] **Step 1: Write the failing tests**

`tests/test_swarm.py`:

```python
OK_CRAWL = {"ok": True, "final_url": URL, "html": LONG_HTML, "text": "t", "sha256": "0" * 64}
REWIND = "⏪ Rewinding the Scout to its clean checkpoint…"


@respx.mock
async def test_scout_job_narrates_its_steps(conn, public_dns):
    mock_api(httpx.Response(200, content=framed(OK_CRAWL)))
    stages = []
    sw = make(conn)
    await sw.scout(URL, on_stage=stages.append)
    await sw.aclose()
    assert stages == ["🔒 Locking the Scout's network to acme.test…", "🌐 Reading the page in a quarantined browser…", REWIND]


@respx.mock
async def test_a_stage_callback_that_raises_never_fails_the_job(conn, public_dns):
    mock_api(httpx.Response(200, content=framed(OK_CRAWL)))

    def boom(text):
        raise RuntimeError("telegram is down")

    sw = make(conn)
    assert (await sw.scout(URL, on_stage=boom)).ok
    await sw.aclose()


@respx.mock
async def test_the_rewind_is_narrated_even_when_the_job_fails(conn, public_dns):
    mock_api(httpx.ConnectError("down"))
    stages = []
    sw = make(conn)
    with pytest.raises(Exception):
        await sw.scout(URL, on_stage=stages.append)
    await sw.aclose()
    assert stages[-1] == REWIND


def test_stage_wording():
    assert swarm.lock_stage("advocate", ["api.sprites.dev"]) == "🔒 Locking the Advocate's network to its connectors only…"
    assert swarm.lock_stage("advocate", []) == "🔒 Closing the Advocate's network…"
    assert swarm.restore_stage("advocate") == "⏪ Rewinding the Advocate to its clean checkpoint…"
```

`tests/test_agent.py` (add `from types import SimpleNamespace`):

```python
LONG_OLD = OLD_HTML.replace("</body>", "<p>" + "This sentence pads the page. " * 40 + "</p></body>")


class NarratingScout:
    """A Scout that says one line and renders LONG_OLD."""

    def available(self, role):
        return True

    async def scout(self, url, *, screenshot=False, extra_hosts=(), on_stage=None):
        on_stage("🔒 Locking the Scout's network to acme.test…")
        return agent.swarm.ScoutResult(ok=True, final_url=url, html=LONG_OLD, text="t", sha256="0" * 64)

    async def aclose(self):
        pass


async def test_a_page_that_needs_the_scout_narrates_it(monkeypatch):
    async def blocked(url, client=None):
        raise httpx.ConnectError("bot wall")

    monkeypatch.setattr(agent, "fetch", blocked)
    cl = SimpleNamespace(swarm=NarratingScout(), exa=None, llama=None, ai=None, pinecone=None)
    stages = []
    page = await agent.page_text(cl, "https://acme.test/terms", on_stage=stages.append)
    assert stages == ["🔒 Locking the Scout's network to acme.test…"] and len(page) >= 400


async def test_link_checks_and_first_watches_pass_the_bubble_down(conn, members, monkeypatch):
    seen = []

    async def page(cl, url, **kw):
        seen.append(kw.get("on_stage"))
        return text(OLD_HTML)

    async def nothing(*args, **kw):
        return []

    async def no_companions(terms_url, client=None):
        return {}

    async def no_grade(q, client=None):
        return None

    monkeypatch.setattr(agent, "page_text", page)
    monkeypatch.setattr(agent, "backfill", nothing)
    monkeypatch.setattr(agent.discover, "discover_companions", no_companions)
    monkeypatch.setattr(agent.tosdr, "lookup", no_grade)
    cl = SimpleNamespace(swarm=None, exa=None, llama=None, ai=None, pinecone=None)
    bubble = [].append
    await agent.check_url(cl, "https://acme.test/terms", on_stage=bubble)
    await agent.subscribe(conn, cl, members[0], "Acme", "https://acme.test/terms", on_stage=bubble)
    assert seen == [bubble, bubble]
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_swarm.py -k "narrat or stage" tests/test_agent.py -k "narrates or bubble"`
Expected: FAIL (`TypeError: Swarm.scout() got an unexpected keyword argument 'on_stage'`).

- [ ] **Step 3: Implement**

In `app/swarm.py`, call the callback through one helper that swallows every exception. Telemetry (`_event`, `_count`) is unchanged. In `app/agent.py`, add the keyword and pass it along; nothing else about reading a page changes. Where a test fake of `page_text` or `scout` elsewhere in `tests/` rejects the new keyword, give that fake `**kw`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/swarm.py app/agent.py tests/test_swarm.py tests/test_agent.py
git commit -m "Have worker jobs narrate their steps to the member who is waiting"
```

---

### Task 2: Inspector buttons: the diff, and how it was checked

**Files:**
- Create: `app/inspector.py`, `tests/test_inspector.py`
- Modify: `app/watch.py` (new `redline_html`), `app/agent.py` (`alert`, new `inspector_rows`), `app/bot.py` (`_dispatch`)
- Test: `tests/test_inspector.py`, `tests/test_watch.py`, `tests/test_agent.py`, `tests/test_bot.py`, `tests/test_e2e_smoke.py`

**Interfaces:**
- Produces:
  - `watch.redline_html(before: str, after: str, limit: int = 3200) -> str`: Telegram HTML word diff built from the same word comparison as `redline`. Removed words are wrapped in `<s>`, added words in `<b>`, every word is escaped with `html.escape`, long unchanged runs are elided as `redline` does. At most `limit` characters of words are emitted; when cut, it ends with ` …` and every tag is closed.
  - `agent.inspector_rows(change_id: int) -> Buttons`: `[[("🔍 What changed", "v1:cd:<id>"), ("🔬 How I checked", "v1:ch:<id>")]]`.
  - `agent.alert(report, svc)` button rows, in order: the inspector row, the opt-out row (when offered), the unwatch row.
  - `inspector.watched_change(conn, user: User, change_id: int) -> tuple[Change, Service] | None`: `None` unless `user` has a subscription to the change's service (the join `advocate.draft_change` uses).
  - `inspector.diff_message(conn, change: Change, svc: Service) -> str`.
  - `inspector.how_checked(conn, change: Change, svc: Service) -> str`.

Exact copy:

- `diff_message`: `<b>What changed in {source} · {heading}</b>` (source is `agent.doc_name(svc.name, role)`; heading falls back to `the terms`), a new line, `<blockquote expandable>{redline_html}</blockquote>`, a new line, `<i><s>Struck through</s> was removed; <b>bold</b> was added.</i>`
- `how_checked`, one line each, in this order:

| Line | Text |
|---|---|
| title | `<b>🔬 How I checked this</b>` |
| source, by the origin of the change's `to_snap` snapshot | `live`: `Read from the live page on 28 Nov 2026 at 08:14 UTC.` · `ota`: `From the Open Terms Archive: the version of 28 Nov 2026.` · `wayback`: `From the Wayback Machine: <a href="{capture}">the capture of 28 Nov 2026</a>.` |
| compared (only when the change has a `from_snap`) | `Compared with the version of 12 Oct 2026.` |
| judge | `Jev's answers (TypeSafe's decision model):` |
| answers | `• Would you want to know? yes, 91% sure` (`no, 80% sure` below 0.5) · `• About: arbitration` · `• Favours: the company` / `you` / `neither side` · `• How much it matters: Major (2.6 of 3)` · `• A way to opt out? yes, 88% sure` |
| partial (only when `change.partial`) | `• Large change: only part of it was judged.` |
| rule | `I alert when Jev is at least 70% sure a change matters.` (from `settings.material_threshold`) |
| unjudged (replaces judge, answers and rule when `change.material is None`) | `This change was not judged: no judge was configured.` |

  The Wayback capture address is `https://web.archive.org/web/{taken_at:%Y%m%d%H%M%S}/{page url}`, where the page URL is the changed document's URL (`service_documents.url` for the change's `document_id`, else `svc.terms_url`). Escape it for an attribute (`html.escape(…, quote=True)`). Dates use the snapshot's `taken_at`.

- [ ] **Step 1: Write the failing tests**

`tests/test_watch.py`:

```python
def test_redline_html_marks_escapes_and_balances():
    out = redline_html("Disputes go to <court> in Delaware.", "Disputes go to arbitration & only arbitration in Delaware.")
    assert "<s>&lt;court&gt;</s>" in out and "<b>arbitration &amp; only arbitration</b>" in out
    assert out.count("<s>") == out.count("</s>") and out.count("<b>") == out.count("</b>")


def test_redline_html_cuts_a_huge_change_cleanly():
    out = redline_html("", "word " * 30_000, limit=3200)
    assert out.endswith(" …</b>") and len(out) < 3300 and out.count("<b>") == out.count("</b>") == 1
```

`tests/test_inspector.py` (new). Write a helper `seed(conn, origin="live", url="https://acme.test/terms")` that returns `(a, b, svc, change)` after creating, with direct calls and SQL: an owner on chat 1 (`users.claim_owner`), members A on chat 2 and B on chat 3 (`users.join_member`); the service `Acme` at `url` (`agent.add_service`) with only A subscribed; two snapshots of it, the first taken `2026-10-12T09:00:00+00:00` and the second `2026-11-28T08:14:00+00:00`, both with the given `origin`; and one change from the first to the second with `heading="3. Arbitration"`, `kind="modified"`, a sentence each for `before_text` and `after_text`, `effective_at` equal to the second snapshot's time, `material=0.91`, `category="arbitration"`, `opt_out=0.88`, `severity=2.6`, `favours="company"`.

```python
def test_only_a_watcher_can_open_a_change(conn):
    a, b, svc, change = seed(conn)
    assert inspector.watched_change(conn, a, change.id)[1].id == svc.id
    assert inspector.watched_change(conn, b, change.id) is None
    assert inspector.watched_change(conn, a, 999) is None


def test_how_checked_for_a_change_from_the_wayback_machine(conn):
    a, b, svc, change = seed(conn, origin="wayback")
    assert inspector.how_checked(conn, change, svc).splitlines() == [
        "<b>🔬 How I checked this</b>",
        'From the Wayback Machine: <a href="https://web.archive.org/web/20261128081400/https://acme.test/terms">the capture of 28 Nov 2026</a>.',
        "Compared with the version of 12 Oct 2026.",
        "Jev's answers (TypeSafe's decision model):",
        "• Would you want to know? yes, 91% sure",
        "• About: arbitration",
        "• Favours: the company",
        "• How much it matters: Major (2.6 of 3)",
        "• A way to opt out? yes, 88% sure",
        "I alert when Jev is at least 70% sure a change matters.",
    ]


def test_how_checked_names_the_live_page_and_the_open_terms_archive(conn):
    a, b, svc, change = seed(conn)
    assert inspector.how_checked(conn, change, svc).splitlines()[1] == "Read from the live page on 28 Nov 2026 at 08:14 UTC."
    conn.execute("UPDATE snapshots SET origin = 'ota' WHERE id = ?", (change.to_snap,))
    assert inspector.how_checked(conn, change, svc).splitlines()[1] == "From the Open Terms Archive: the version of 28 Nov 2026."


def test_how_checked_escapes_the_archived_address(conn):
    a, b, svc, change = seed(conn, origin="wayback", url='https://acme.test/terms?a=1&b="x"')
    line = inspector.how_checked(conn, change, svc).splitlines()[1]
    assert 'href="https://web.archive.org/web/20261128081400/https://acme.test/terms?a=1&amp;b=&quot;x&quot;"' in line


def test_how_checked_leaves_out_what_does_not_apply(conn):
    a, b, svc, change = seed(conn)
    conn.execute("UPDATE changes SET from_snap = NULL, partial = 1 WHERE id = ?", (change.id,))
    [first] = db.rows(db.Change, conn.execute("SELECT * FROM changes"))
    lines = inspector.how_checked(conn, first, svc).splitlines()
    assert not any(line.startswith("Compared with") for line in lines) and "• Large change: only part of it was judged." in lines
    conn.execute("UPDATE changes SET material = NULL, category = NULL WHERE id = ?", (change.id,))
    [unjudged] = db.rows(db.Change, conn.execute("SELECT * FROM changes"))
    assert inspector.how_checked(conn, unjudged, svc).splitlines()[-1] == "This change was not judged: no judge was configured."


def test_diff_message_escapes_the_heading(conn):
    a, b, svc, change = seed(conn)
    conn.execute("UPDATE changes SET heading = 'Fees & <Charges>' WHERE id = ?", (change.id,))
    [fresh] = db.rows(db.Change, conn.execute("SELECT * FROM changes"))
    out = inspector.diff_message(conn, fresh, svc)
    assert out.startswith("<b>What changed in Acme · Fees &amp; &lt;Charges&gt;</b>\n<blockquote expandable>")
    assert out.endswith("<i><s>Struck through</s> was removed; <b>bold</b> was added.</i>")
```

`tests/test_bot.py` (import `seed` from `tests.test_inspector`; it creates the users, so no `/start` is needed):

```python
def tap(chat, data, callback_id="cb"):
    return Incoming(chat_id=chat, kind="tap", data=data, callback_id=callback_id, message_id=5)


async def test_a_watcher_opens_the_diff_and_a_stranger_gets_nothing(conn, tg):
    a, b, svc, change = seed(conn)
    await bot.handle(conn, None, tg, tap(a.chat_id, f"v1:cd:{change.id}"))
    assert tg.to(a.chat_id)[-1].startswith("<b>What changed in Acme")
    await bot.handle(conn, None, tg, tap(a.chat_id, f"v1:ch:{change.id}"))
    assert tg.to(a.chat_id)[-1].startswith("<b>🔬 How I checked this</b>")
    before = len(tg.sent)
    await bot.handle(conn, None, tg, tap(b.chat_id, f"v1:cd:{change.id}"))
    await bot.handle(conn, None, tg, tap(b.chat_id, f"v1:ch:{change.id}"))
    assert len(tg.sent) == before and [c[2] for c in tg.called("answer")][-2:] == [bot.STALE_TAP] * 2
```

`tests/test_agent.py`: one test that `agent.alert` orders its rows as inspector, opt-out, unwatch. `tests/test_e2e_smoke.py`: after the arbitration alert reaches member A, assert its first button row is `["🔍 What changed", "🔬 How I checked"]`, that A's `cd` tap is answered with the diff, and that B's is answered with `bot.STALE_TAP`.

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_inspector.py tests/test_watch.py -k "redline_html or watcher or how_checked or diff_message"`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.inspector'`).

- [ ] **Step 3: Implement**

`watch.redline_html`, the `agent` changes, `app/inspector.py`, and two `_dispatch` arms (`cd`, `ch`). Each arm calls `inspector.watched_change` first: on `None` it answers `STALE_TAP`; otherwise it answers the callback and sends the message. Existing tests that pinned the old button rows of an alert are updated to the new order.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/inspector.py app/watch.py app/agent.py app/bot.py tests/
git commit -m "Add inspector buttons: the diff and how a change was checked"
```

---

### Task 3: Open a past change

**Files:**
- Modify: `app/inspector.py` (new `full_card`, `change_list`), `app/bot.py` (`_dispatch`, `_watch`)
- Test: `tests/test_inspector.py`, `tests/test_bot.py`

**Interfaces:**
- Consumes: `inspector.watched_change`, `agent.report_for`, `agent.alert`, `agent.past_material_changes`.
- Produces:
  - `async inspector.full_card(conn, cl, user, change_id: int) -> Outgoing | None`: the change as `agent.alert(await agent.report_for(conn, cl, change, svc, user, {}, {}), svc)`; `None` for a non-watcher.
  - `inspector.change_list(conn, user, service_id: int) -> Outgoing | None`: `None` unless the member watches the service. Otherwise the newest 10 of `past_material_changes`, one button per row, label `{effective_at:%d %b} · {heading}` cut to 40 characters with `…`, data `co:<id>`. Text: `<b>Acme</b>: 3 material changes in the past year. Tap one to open it.` (singular `change`); with none: `No material changes to Acme in the past year.` and no buttons.
  - The `/watch` reply's buttons, when it has past changes: `[[("🔎 Open a change", "v1:cl:<service id>")], [unwatch]]`.

This is how the video shows a real company's change, so it is not optional.

- [ ] **Step 1: Write the failing tests**

`tests/test_inspector.py`. `past_material_changes` looks back 365 days from now, so this test uses dates relative to now rather than the fixed dates in `seed`:

```python
def test_change_list_shows_the_newest_ten_to_a_watcher_only(conn):
    a, b, svc, change = seed(conn)
    now = datetime.now(UTC)
    conn.execute("UPDATE changes SET effective_at = ? WHERE id = ?", ((now - timedelta(days=1)).isoformat(), change.id))
    for n in range(11):
        conn.execute(
            "INSERT INTO changes (service_id, from_snap, to_snap, detected_at, effective_at, heading, kind, before_text,"
            " after_text, material) VALUES (?, ?, ?, ?, ?, ?, 'modified', 'x', 'y', 0.9)",
            (svc.id, change.from_snap, change.to_snap, db.now(), (now - timedelta(days=30 + n)).isoformat(),
             f"Section {n} with a very long heading indeed"))
    out = inspector.change_list(conn, a, svc.id)
    assert out.text == "<b>Acme</b>: 12 material changes in the past year. Tap one to open it."
    assert len(out.buttons) == 10 and out.buttons[0][0][0] == f"{now - timedelta(days=1):%d %b} · 3. Arbitration"
    assert all(len(row[0][0]) <= 40 and row[0][1].startswith("v1:co:") for row in out.buttons)
    assert inspector.change_list(conn, b, svc.id) is None


async def test_full_card_carries_the_inspector_and_opt_out_buttons(conn):
    a, b, svc, change = seed(conn)
    cl = SimpleNamespace(pinecone=None, ai=None, swarm=None)
    card = await inspector.full_card(conn, cl, a, change.id)
    labels = [label for row in card.buttons for label, _ in row]
    assert labels == ["🔍 What changed", "🔬 How I checked", "✉️ Review & Draft Opt-Out", "🗑 Unwatch Acme"]
    assert await inspector.full_card(conn, cl, b, change.id) is None
```

`tests/test_bot.py`: `co` by A sends a card whose text starts `<b>Acme</b> changed`; `co` and `cl` by B answer `bot.STALE_TAP` and send nothing; `cl` by A sends the list. Pass `SimpleNamespace(pinecone=None, ai=None, swarm=None)` as the clients for the `co` tap.

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_inspector.py -k "change_list or full_card"`
Expected: FAIL (`AttributeError: module 'app.inspector' has no attribute 'change_list'`).

- [ ] **Step 3: Implement**

The two functions, two `_dispatch` arms (decide stale or not before answering the callback; answer `Opening…` for `co`), and the extra button row in `_watch`. Send cards with `notify.send_outgoing(conn, tg, chat, card)` so a rated card gets its 👍/👎 row.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/inspector.py app/bot.py tests/test_inspector.py tests/test_bot.py
git commit -m "Let a watcher open any past change as a full card"
```

---

### Task 4: The redline kit narrates its three jobs

**Files:**
- Modify: `app/advocate.py` (`redline_kit`, `_passes`), `app/bot.py` (the `rt` arm of `_dispatch`)
- Test: `tests/test_advocate.py`, `tests/test_bot.py`

**Interfaces:**
- Consumes: `Swarm.run_job(on_stage=)` (Task 1), `progress.working`.
- Produces: `advocate.redline_kit(conn, cl, tg, user, ref, tone_code, on_stage: Stage = no_stage) -> str` and `_passes(cl, entry, tone, on_stage: Stage = no_stage)`; all three `run_job` calls receive `on_stage`.

- [ ] **Step 1: Write the failing tests**

`tests/test_advocate.py`: give `FakeSwarm.run_job` one more keyword argument, `on_stage=None`, and have it append `on_stage` to a new list `self.stage_callbacks`. Then, with the file's existing `kit_swarm` and `held` helpers:

```python
async def test_redline_kit_hands_its_stage_callback_to_all_three_jobs(conn, members, monkeypatch):
    a = members[0]
    sw, tg, stages = kit_swarm(monkeypatch), FakeTelegram(), []
    ref = held(a)
    assert await advocate.redline_kit(conn, SimpleNamespace(swarm=sw, ai=None), tg, a, ref, "b", on_stage=stages.append) == "ok"
    assert sw.kinds() == ["counterparty", "consumer", "kit"] and sw.stage_callbacks == [stages.append] * 3
```

`tests/test_bot.py`:

```python
async def test_the_redline_tap_runs_inside_a_working_bubble(conn, tg, monkeypatch):
    await say(conn, tg, 1, "/start owner-code")

    async def fake_kit(conn_, cl_, tg_, user, ref, tone, on_stage=None):
        on_stage("⚖️ The other side's counsel is reading the contract…")
        return "ok"

    monkeypatch.setattr(bot.advocate, "redline_kit", fake_kit)
    await bot.handle(conn, None, tg, Incoming(chat_id=1, kind="tap", data=f"v1:rt:{'A' * 22}:b", message_id=5))
    assert ("draft", 1, "⚖️ The other side's counsel is reading the contract…") in tg.calls
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_bot.py::test_the_redline_tap_runs_inside_a_working_bubble`
Expected: FAIL (`fake_kit` is called without `on_stage`, so `on_stage(...)` raises `TypeError`).

- [ ] **Step 3: Implement**

Thread `on_stage` through `redline_kit` and `_passes`. In the `rt` arm, wrap the call in `async with progress.working(tg, user.chat_id, None) as w:` and set `w.failed = outcome != "ok"`. The kit's own messages are sent from inside the bubble; that is acceptable here because the function returns right after them.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/advocate.py app/bot.py tests/test_advocate.py tests/test_bot.py
git commit -m "Narrate the redline kit's three Advocate jobs"
```

# Lane 1: Inspector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner trigger one service's check on demand, and let any watcher open a change and see its diff, its evidence screenshot and how it was judged, while the Sprites narrate their own steps.

**Architecture:** The daily run's per-service body becomes one function shared by the cron, `/check` and the email path. The Swarm reports each step through the existing progress callback and fills a small trace; the trace is stored on the snapshot as provenance. A new `app/inspector.py` renders stored facts only (no provider calls) behind access-checked buttons.

**Tech Stack:** Python 3.12, SQLite, Pydantic, httpx, respx, pytest (asyncio auto mode).

**Spec:** `docs/superpowers/specs/2026-11-28-show-the-work-and-census-design.md`, sections 1.1 to 1.5.

## Global Constraints

- All of `docs/superpowers/plans/2026-11-28-show-the-work-and-census.md` "Global Constraints" apply.
- Lane 0 is already on your branch: `snapshots.provenance`, the button data `cd`, `ce`, `ch`, `co`, `cl`, and `FakeTelegram.send_document` and `watch()` in `tests/test_bot.py`.
- Do not touch `advocate.approve`, `advocate.finish`, `_printable` or `bot._name_for` (Lane 4 owns them).
- Narration and provenance are cosmetic and best effort: they never raise and never change what a check returns.
- `app/agent.py` must not import `app/inspector.py` (the inspector imports the agent).

## Review Focus

1. **A tap after a restart or a wiped disk:** `evidence_path` is stored but the file is gone. Expected: the tap answers "That screenshot is no longer available." and sends nothing. (Task 6)
2. **Unsafe or oversized diff text:** `<`, `&` and a 30,000-word section. Expected: escaped, cut at a word, tags balanced. (Task 6)
3. **A non-watcher taps a forged id** for another member's service. Expected: `bot.STALE_TAP`, nothing sent. (Tasks 6 and 7)
4. **`/check` on a name nobody watches, or with no name.** Expected: one sentence listing what is watched. (Task 2)
5. **The Scout is down or not configured** when evidence is wanted. Expected: the alert goes out without the Evidence button. (Task 5)

---

### Task 1: One function for "check a service and tell its watchers"

**Files:**
- Modify: `app/agent.py` (new `ServiceReport`, `check_and_report`; `tick` calls it), `app/intake.py` (`_terms_update` calls it)
- Test: `tests/test_agent.py`, and the test file that covers `intake._terms_update`

**Interfaces:**
- Produces:
  - `agent.ServiceReport(BaseModel)`: `changes: int = 0`, `contradictions: int = 0`, `deliveries: dict[int, list[Outgoing]]` (user id to messages).
  - `async agent.check_and_report(conn, cl, svc: Service, on_stage: Stage = no_stage) -> ServiceReport`. It runs `check_bundle`, then for each change and each of `subscribers(conn, svc.id)` builds `alert(await report_for(conn, cl, change, svc, user, vectors, benchmarks), svc)` with one shared `vectors` and one shared `benchmarks` dict, then each `contradiction_alert`. It lets `check_bundle`'s exceptions through. `on_stage` is unused until Task 4.

- [ ] **Step 1: Write the failing test** in `tests/test_agent.py`

```python
@respx.mock
async def test_check_and_report_tells_each_watcher_once(conn, members, monkeypatch):
    mock_providers()
    a, b, c = members
    pages = {"text": text(OLD_HTML)}

    async def page(cl, url, **kw):
        return pages["text"]

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
    cl = agent.Clients()
    svc, _ = await agent.subscribe(conn, cl, a, "Acme", "https://acme.test/terms")
    await agent.subscribe(conn, cl, b, "Acme", "https://acme.test/terms")
    pages["text"] = text(NEW_HTML)
    report = await agent.check_and_report(conn, cl, svc)
    await cl.aclose()
    assert (report.changes, report.contradictions) == (2, 0)   # "2. Payments" and "3. Arbitration"
    assert set(report.deliveries) == {a.id, b.id}              # c does not watch Acme
    assert all(len(messages) == 2 for messages in report.deliveries.values())
```

Add one test beside the existing `_terms_update` tests: monkeypatch `agent.check_and_report` to return a `ServiceReport` with one message for the sender and one for a second watcher; assert the sender's returned cards contain theirs and the second watcher's arrives through `deliver`.

- [ ] **Step 2: Run and see it fail**

Run: `uv run pytest -q tests/test_agent.py::test_check_and_report_tells_each_watcher_once`
Expected: FAIL (`AttributeError: module 'app.agent' has no attribute 'check_and_report'`).

- [ ] **Step 3: Implement**

Add `ServiceReport` and `check_and_report` to `app/agent.py`. Make `tick` loop over it (keeping its per-service `try`/`except` and `TickResult` counts), and make `intake._terms_update` use it: the sender's own messages (`deliveries.pop(user.id, [])`) join the reply cards, the rest go through `deliver`. The email path now gets benchmark lines, which it lacked.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass; the existing `tick` tests are unchanged.

- [ ] **Step 5: Commit**

```bash
git add app/agent.py app/intake.py tests/
git commit -m "Share one check-and-report function between the daily run and email"
```

---

### Task 2: `/check <service>` for the owner

**Files:**
- Modify: `app/bot.py` (`OWNER_MENU`, `OWNER_HELP`, `handle`, new `_check`), `app/agent.py` (new `watched_service_named`)
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `agent.check_and_report` (Task 1), `notify.deliver`, `progress.working`.
- Produces: `agent.watched_service_named(conn, name: str) -> Service | None` (a member of `watched_services(conn)` whose name equals `name` ignoring case and surrounding spaces).

Exact replies (the name is escaped):

| Case | Reply to the owner |
|---|---|
| changes found | `Checked <b>Acme</b>: 1 material change, 0 contradictions, 1 member notified.` (plurals: `changes`, `contradiction`, `members`) |
| nothing new | `Checked <b>Acme</b>: no changes since 27 Nov.` (date of the newest snapshot, `%d %b`; `no changes.` if there is none) |
| unknown name or no name | `I'm not watching anything called nothing. Watched: Acme, Notion.` / `Usage: /check &lt;service&gt;. Watched: Acme, Notion.` (sent escaped, so the member reads `<service>`) |
| nothing watched at all | `Nothing is being watched yet.` |
| check raised | `Couldn't check <b>Acme</b>: ConnectError.` (the exception's type name only) |

- [ ] **Step 1: Write the failing tests** in `tests/test_bot.py` (add `from app.outgoing import Outgoing`; `watch` is the helper Lane 0 put in this file)

```python
async def test_check_is_owner_only(conn, tg):
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/check acme")
    assert tg.to(2)[-1] == "That command is owner-only."


async def test_check_runs_one_service_and_tells_its_watchers(conn, tg, monkeypatch):
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 2, "/start join-code")
    member = users.by_chat(conn, 2)
    svc = watch(conn, member)
    seen = []

    async def fake(conn_, cl_, s, on_stage=None):
        seen.append(s.id)
        return agent.ServiceReport(changes=1, deliveries={member.id: [Outgoing(text="<b>Acme</b> changed")]})

    monkeypatch.setattr(agent, "check_and_report", fake)
    await say(conn, tg, 1, "/check ACME")
    assert seen == [svc.id]
    assert tg.to(2)[-1] == "<b>Acme</b> changed"
    assert tg.to(1)[-1] == "Checked <b>Acme</b>: 1 material change, 0 contradictions, 1 member notified."
    assert tg.called("draft")  # it ran inside a progress bubble


async def test_check_with_nothing_new_says_since_when(conn, tg, monkeypatch):
    await say(conn, tg, 1, "/start owner-code")
    svc = watch(conn, users.owner(conn))
    conn.execute("INSERT INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (?, '2026-11-27T09:00:00+00:00', 'live', 'x', 't')", (svc.id,))

    async def quiet(conn_, cl_, s, on_stage=None):
        return agent.ServiceReport()

    monkeypatch.setattr(agent, "check_and_report", quiet)
    await say(conn, tg, 1, "/check acme")
    assert tg.to(1)[-1] == "Checked <b>Acme</b>: no changes since 27 Nov."


async def test_check_of_an_unknown_name_lists_what_is_watched(conn, tg):
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 1, "/check nothing")
    assert tg.to(1)[-1] == "Nothing is being watched yet."
    watch(conn, users.owner(conn))
    await say(conn, tg, 1, "/check nothing")
    assert tg.to(1)[-1] == "I'm not watching anything called nothing. Watched: Acme."
    await say(conn, tg, 1, "/check")
    assert tg.to(1)[-1] == "Usage: /check &lt;service&gt;. Watched: Acme."


async def test_check_that_fails_names_only_the_error_type(conn, tg, monkeypatch):
    await say(conn, tg, 1, "/start owner-code")
    watch(conn, users.owner(conn))

    async def boom(conn_, cl_, s, on_stage=None):
        raise httpx.ConnectError("https://acme.test/terms refused")

    monkeypatch.setattr(agent, "check_and_report", boom)
    await say(conn, tg, 1, "/check acme")
    assert tg.to(1)[-1] == "Couldn't check <b>Acme</b>: ConnectError."
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_bot.py -k check`
Expected: FAIL (`/check` is not a command, so the owner gets the "Not sure what you mean" pointer).

- [ ] **Step 3: Implement**

`OWNER_MENU` gains `("check", "Check one service's terms now")` and `OWNER_HELP` a matching line. In `handle`, `/check` joins the owner-only branch. `_check` resolves the service, runs `check_and_report` inside `progress.working(tg, chat, msg.message_id)` with a first stage of `Checking Acme…`, delivers with `notify.deliver(conn, tg, cl, report.deliveries, f"Fine Print: {svc.name} changed its terms")`, then replies after the block.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/bot.py app/agent.py tests/test_bot.py
git commit -m "Let the owner check one service's terms on demand"
```

---

### Task 3: The Swarm narrates its steps and times them

**Files:**
- Modify: `app/swarm.py` (`job`, `run_job`, `scout`; new `JobTrace`, `lock_stage`, `restore_stage`, `RUN_STAGES`)
- Test: `tests/test_swarm.py`

**Interfaces:**
- Produces:
  - `swarm.JobTrace(BaseModel)`: `role: str = ""`, `hosts: int = 0`, `lock_ms: int | None = None`, `run_ms: int | None = None`, `restore_ms: int | None = None` (mutable; the job fills it).
  - `Swarm.job(role, allow_hosts, *, on_stage: Stage = no_stage, trace: JobTrace | None = None)`, `Swarm.run_job(..., on_stage: Stage = no_stage, trace: JobTrace | None = None)`, `Swarm.scout(url, *, screenshot=False, extra_hosts=(), on_stage: Stage = no_stage, trace: JobTrace | None = None)`. `Stage` and `no_stage` come from `app.progress`.
  - `swarm.lock_stage(role: str, allow_hosts: Sequence[str]) -> str` and `swarm.restore_stage(role: str) -> str`.

Exact stage texts:

| When | Text |
|---|---|
| lock, hosts given | `🔒 Locking the Scout's network to acme.test…` (the first host) |
| lock, hosts equal `IDLE_HOSTS[role]` and not empty | `🔒 Locking the Advocate's network to its connectors only…` |
| lock, empty list | `🔒 Closing the Advocate's network…` |
| run, by `detail` | `playwright_crawl`: `🌐 Reading the page in a quarantined browser…`; `counterparty_counsel`: `⚖️ The other side's counsel is reading the contract…`; `consumer_counsel`: `🛡 Your counsel is drafting the changes…`; `redline_build`: `📝 Building the tracked-changes document…`; anything else: `⚙️ Working on the Advocate…` |
| restore | `⏪ Rewinding the Scout to its clean checkpoint…` |

No lock stage when `allow_hosts is None`. The restore stage is announced before the restore starts, including when the job failed.

- [ ] **Step 1: Write the failing tests** in `tests/test_swarm.py`

```python
OK_CRAWL = {"ok": True, "final_url": URL, "html": LONG_HTML, "text": "t", "sha256": "0" * 64}
REWIND = "⏪ Rewinding the Scout to its clean checkpoint…"


@respx.mock
async def test_scout_job_reports_its_steps_and_timings(conn, public_dns):
    mock_api(httpx.Response(200, content=framed(OK_CRAWL)))
    stages, trace = [], swarm.JobTrace()
    sw = make(conn)
    await sw.scout(URL, on_stage=stages.append, trace=trace)
    await sw.aclose()
    assert stages == ["🔒 Locking the Scout's network to acme.test…", "🌐 Reading the page in a quarantined browser…", REWIND]
    assert (trace.role, trace.hosts) == ("scout", len(swarm.egress_hosts(URL)))
    assert all(isinstance(ms, int) and ms >= 0 for ms in (trace.lock_ms, trace.run_ms, trace.restore_ms))


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


def test_lock_stage_wording():
    assert swarm.lock_stage("advocate", ["api.sprites.dev"]) == "🔒 Locking the Advocate's network to its connectors only…"
    assert swarm.lock_stage("advocate", []) == "🔒 Closing the Advocate's network…"
    assert swarm.restore_stage("advocate") == "⏪ Rewinding the Advocate to its clean checkpoint…"
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_swarm.py -k "steps_and_timings or stage or rewind"`
Expected: FAIL (`TypeError: Swarm.scout() got an unexpected keyword argument 'on_stage'`).

- [ ] **Step 3: Implement in `app/swarm.py`**

Call the callback through one helper that swallows every exception. `job` fills `lock_ms` and `restore_ms` (it already measures both) and `hosts`; `run_job` measures the exec and fills `run_ms`, and passes both arguments to `job`. Telemetry (`_event`, `_count`) is unchanged.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/swarm.py tests/test_swarm.py
git commit -m "Have worker jobs narrate their steps and report their timings"
```

---

### Task 4: Provenance on every live version

**Files:**
- Modify: `app/agent.py` (`scout_text`, `page_text`, `record_version`, `check_bundle`, `_onboard_document`, `onboard_service`, `subscribe`, `check_and_report`, `check_url`; new `PageTrace`, `provenance`)
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `swarm.JobTrace`, `Swarm.scout(on_stage=, trace=)` (Task 3), `settings.judge_model`.
- Produces:
  - `agent.PageTrace(BaseModel)`: `via: Literal["direct", "scout", "exa", "llama"] | None = None`, `scout: swarm.JobTrace | None = None`.
  - `page_text(cl, url, *, public=None, may_scout=None, links=None, on_stage: Stage = no_stage, trace: PageTrace | None = None) -> str`: sets `trace.via` to the step that produced the returned text, and `trace.scout` whenever the Scout ran.
  - `agent.provenance(trace: PageTrace | None, url: str, cl: Clients) -> dict | None`: `None` when there is no trace or `trace.via is None`; otherwise `{"via": …, "host": <url host without "www.">, "judge_model": settings.judge_model}` plus `"scout": {"hosts", "lock_ms", "run_ms", "restore_ms"}` when the Scout ran.
  - `record_version(conn, cl, svc, text, taken_at, origin, doc=None, *, provenance: dict | None = None)`: stores it as compact JSON in `snapshots.provenance` (NULL when `None`).
  - `check_bundle(conn, cl, svc, on_stage: Stage = no_stage)`, `onboard_service(conn, cl, svc, on_stage: Stage = no_stage)`: pass `on_stage` and a fresh `PageTrace` per document to `page_text`. `subscribe` passes its `on_stage` on; `check_and_report` passes its own to `check_bundle`.

- [ ] **Step 1: Write the failing tests** in `tests/test_agent.py`

```python
LONG_OLD = OLD_HTML.replace("</body>", "<p>" + "This sentence pads the page. " * 40 + "</p></body>")


def quiet_world(monkeypatch):
    async def nothing(*args, **kw):
        return []

    async def no_companions(terms_url, client=None):
        return {}

    async def no_grade(q, client=None):
        return None

    monkeypatch.setattr(agent, "backfill", nothing)
    monkeypatch.setattr(agent.discover, "discover_companions", no_companions)
    monkeypatch.setattr(agent.tosdr, "lookup", no_grade)


@respx.mock
async def test_a_live_version_records_how_it_was_read(conn, members, monkeypatch):
    mock_providers()
    quiet_world(monkeypatch)

    async def fetched(url, client=None):
        return "text/html", LONG_OLD

    monkeypatch.setattr(agent, "fetch", fetched)
    cl = agent.Clients()
    await agent.subscribe(conn, cl, members[0], "Acme", "https://www.acme.test/terms")
    await cl.aclose()
    [snap] = db.rows(db.Snapshot, conn.execute("SELECT * FROM snapshots WHERE origin = 'live'"))
    assert json.loads(snap.provenance) == {"via": "direct", "host": "acme.test", "judge_model": "jev-1.14"}


class TimedScout:
    """A Scout that renders `html` and reports fixed timings."""

    def __init__(self, html, png=b"", fail=False):
        self.html, self.png, self.fail, self.calls = html, png, fail, []

    def available(self, role):
        return True

    async def scout(self, url, *, screenshot=False, extra_hosts=(), on_stage=None, trace=None):
        self.calls.append((url, screenshot))
        if self.fail:
            raise agent.swarm.SwarmError("timeout")
        if trace is not None:
            trace.role, trace.hosts, trace.lock_ms, trace.run_ms, trace.restore_ms = "scout", 1, 1100, 8400, 15200
        shot = base64.b64encode(self.png).decode() if screenshot and self.png else ""
        return agent.swarm.ScoutResult(ok=True, final_url=url, html=self.html, text="t", sha256="0" * 64, screenshot_png_b64=shot)

    async def aclose(self):
        pass


@respx.mock
async def test_a_version_read_by_the_scout_records_its_timings(conn, members, monkeypatch):
    mock_providers()
    quiet_world(monkeypatch)

    async def blocked(url, client=None):
        raise httpx.ConnectError("bot wall")

    monkeypatch.setattr(agent, "fetch", blocked)
    cl = agent.Clients()
    cl.swarm = TimedScout(LONG_OLD)
    await agent.subscribe(conn, cl, members[0], "Acme", "https://acme.test/terms")
    await cl.aclose()
    [snap] = db.rows(db.Snapshot, conn.execute("SELECT * FROM snapshots WHERE origin = 'live'"))
    assert json.loads(snap.provenance) == {
        "via": "scout", "host": "acme.test", "judge_model": "jev-1.14",
        "scout": {"hosts": 1, "lock_ms": 1100, "run_ms": 8400, "restore_ms": 15200}}


@respx.mock
async def test_archived_versions_and_untraced_reads_have_no_provenance(conn, members, history):
    mock_providers()
    cl = agent.Clients()
    await agent.subscribe(conn, cl, members[0], "Acme", "https://acme.test/terms")
    await cl.aclose()
    assert [s.provenance for s in db.rows(db.Snapshot, conn.execute("SELECT * FROM snapshots"))] == [None, None]
```

Add `import base64` to the test module. The last test uses the existing `history` fixture, whose fake `page_text` fills no trace.

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_agent.py -k "records or provenance"`
Expected: FAIL (`provenance` is `None` where a dict is expected).

- [ ] **Step 3: Implement in `app/agent.py`**

As specified in Interfaces. Where a test fake of `page_text`, `scout` or `backfill` elsewhere in `tests/` rejects the new keyword arguments, give that fake `**kw`; do not narrow the new signatures.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/agent.py tests/
git commit -m "Record how each live version was read"
```

---

### Task 5: Evidence for every material change

**Files:**
- Modify: `app/agent.py` (`check_bundle`; new `capture_evidence`)
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `Swarm.scout(screenshot=True, on_stage=, trace=)`, `swarm.save_evidence`, `swarm.PublicPage`, `TimedScout` from Task 4's tests.
- Produces: `async agent.capture_evidence(conn, cl, svc: Service, doc: ServiceDocument, url: str, on_stage: Stage = no_stage) -> bool`. It acts on the document's newest `live` snapshot. It returns `False` without calling the Scout when that snapshot already has `evidence_path`, when `cl.swarm` is `None` or `not cl.swarm.available("scout")`. Otherwise it crawls with `screenshot=True`, saves the PNG under the snapshot's own `sha`, sets `snapshots.evidence_path`, and merges into the stored provenance: `"scout": {…}` (the timings) and `"evidence": {"at": <UTC ISO seconds>, "png_sha256": <hex>, "text_match": <bool>}`, where `text_match` says whether the Scout's normalised text has the snapshot's `sha`. Any exception is logged by type and returns `False`.
- `check_bundle` calls it once per document whose `record_version` returned at least one change, before it returns.

- [ ] **Step 1: Write the failing tests** in `tests/test_agent.py`

```python
PNG = agent.swarm.PNG_MAGIC + b"evidence-bytes"
LONG_NEW = NEW_HTML.replace("</body>", "<p>" + "This sentence pads the page. " * 40 + "</p></body>")


async def changed_service(conn, members, monkeypatch, scout, material=0.91):
    """Acme watched at LONG_OLD, then its page becomes LONG_NEW; returns (clients, service)."""
    mock_providers(material)
    quiet_world(monkeypatch)
    page = {"html": LONG_OLD}

    async def fetched(url, client=None):
        return "text/html", page["html"]

    monkeypatch.setattr(agent, "fetch", fetched)
    cl = agent.Clients()
    cl.swarm = scout
    svc, _ = await agent.subscribe(conn, cl, members[0], "Acme", "https://acme.test/terms")
    page["html"] = LONG_NEW
    return cl, svc


def newest(conn):
    return db.rows(db.Snapshot, conn.execute("SELECT * FROM snapshots ORDER BY id"))[-1]


@respx.mock
async def test_a_material_change_read_directly_gets_a_screenshot(conn, members, monkeypatch):
    scout = TimedScout(LONG_NEW, PNG)
    cl, svc = await changed_service(conn, members, monkeypatch, scout)
    checked = await agent.check_bundle(conn, cl, svc)
    await cl.aclose()
    snap = newest(conn)
    assert len(checked.changes) == 2 and scout.calls == [("https://acme.test/terms", True)]
    assert (settings.evidence_dir / snap.evidence_path).read_bytes() == PNG
    stored = json.loads(snap.provenance)
    assert stored["via"] == "direct" and stored["scout"]["restore_ms"] == 15200
    assert stored["evidence"]["png_sha256"] == hashlib.sha256(PNG).hexdigest() and stored["evidence"]["text_match"] is True


@respx.mock
async def test_a_failed_screenshot_never_blocks_the_alert(conn, members, monkeypatch):
    cl, svc = await changed_service(conn, members, monkeypatch, TimedScout(LONG_NEW, PNG, fail=True))
    checked = await agent.check_bundle(conn, cl, svc)
    await cl.aclose()
    assert len(checked.changes) == 2 and newest(conn).evidence_path is None


@respx.mock
async def test_no_screenshot_without_a_material_change_or_a_scout(conn, members, monkeypatch):
    scout = TimedScout(LONG_NEW, PNG)
    cl, svc = await changed_service(conn, members, monkeypatch, scout, material=0.1)
    assert (await agent.check_bundle(conn, cl, svc)).changes == [] and scout.calls == []
    cl.swarm = None  # single-Sprite mode: nothing to capture with, and nothing raised
    assert await agent.capture_evidence(conn, cl, svc, agent.documents_for(conn, svc)[0], svc.terms_url) is False
    await cl.aclose()


@respx.mock
async def test_a_version_that_has_a_screenshot_is_not_captured_twice(conn, members, monkeypatch):
    scout = TimedScout(LONG_NEW, PNG)
    cl, svc = await changed_service(conn, members, monkeypatch, scout)
    await agent.check_bundle(conn, cl, svc)
    assert await agent.capture_evidence(conn, cl, svc, agent.documents_for(conn, svc)[0], svc.terms_url) is False
    await cl.aclose()
    assert len(scout.calls) == 1
```

Add `import hashlib` and `from app.config import settings` to the test module if absent.

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_agent.py -k "screenshot"`
Expected: FAIL (`evidence_path` is `None`; `capture_evidence` does not exist).

- [ ] **Step 3: Implement in `app/agent.py`**

As specified. Reuse the text extraction `scout_text` already applies to a `ScoutResult` (extract it into one private helper used by both).

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/agent.py tests/test_agent.py
git commit -m "Capture a Scout screenshot for every material change"
```

---

### Task 6: Inspector buttons: the diff, the evidence, how it was checked

**Files:**
- Create: `app/inspector.py`, `tests/test_inspector.py`
- Modify: `app/watch.py` (new `redline_html`), `app/agent.py` (`ChangeReport.evidence`, `report_for`, `alert`, new `inspector_rows`), `app/bot.py` (`_dispatch`)
- Test: `tests/test_inspector.py`, `tests/test_watch.py`, `tests/test_bot.py`, `tests/test_e2e_smoke.py`

**Interfaces:**
- Produces:
  - `watch.redline_html(before: str, after: str, limit: int = 3200) -> str`: Telegram HTML word diff built from the same word comparison as `redline`. Removed words are wrapped in `<s>`, added words in `<b>`, every word is escaped with `html.escape`, long unchanged runs are elided as `redline` does. At most `limit` characters of words are emitted; when cut, it ends with ` …` and every tag is closed.
  - `agent.ChangeReport.evidence: bool = False`, set by `report_for` (`True` when the change's `to_snap` snapshot has `evidence_path`).
  - `agent.inspector_rows(change_id: int, evidence: bool) -> Buttons`: `[[("🔍 What changed", cd), ("🔬 How I checked", ch)]]`, plus `[("📸 Evidence", ce)]` as a second row when `evidence`.
  - `agent.alert(report, svc)` button rows, in order: the inspector rows, the opt-out row (when offered), the unwatch row.
  - `inspector.watched_change(conn, user: User, change_id: int) -> tuple[Change, Service] | None`: `None` unless `user` has a subscription to the change's service.
  - `inspector.diff_message(conn, change: Change, svc: Service) -> str`.
  - `inspector.evidence_file(conn, change: Change, svc: Service) -> tuple[str, bytes, str] | None`: `(filename, png bytes, caption)`; `None` when there is no path, the file is missing, or the path resolves outside `settings.evidence_dir`. The module imports `settings` at the top (`from app.config import settings`) and reads `settings.evidence_dir` at call time, so a test can replace `inspector.settings`.
  - `inspector.how_checked(conn, change: Change, svc: Service) -> str`.

Exact copy:

- `diff_message`: `<b>What changed in {source} · {heading}</b>` (source is `agent.doc_name(svc.name, role)`; heading falls back to `the terms`), a new line, `<blockquote expandable>{redline_html}</blockquote>`, a new line, `<i><s>Struck through</s> was removed; <b>bold</b> was added.</i>`
- Evidence filename `evidence-{service name lower-cased, non-alphanumerics to "-"}-{taken_at:%Y%m%d}.png`; caption lines:
  1. `📸 <b>{source}</b> as the Scout Sprite saw it on {at:%d %b %Y at %H:%M} UTC.` (`at` is `provenance.evidence.at`, else the snapshot's `taken_at`)
  2. `Text SHA-256: <code>{snapshot sha}</code>`
  3. `PNG SHA-256: <code>{sha256 of the file}</code>`
  4. only when `text_match` is `False`: `The page the Scout rendered differed from the text I judged; sites sometimes serve browsers differently.`
- Missing screenshot: the `ce` tap answers `That screenshot is no longer available.` and sends nothing.
- `how_checked`, one line each, in this order:

| Line | Text |
|---|---|
| title | `<b>🔬 How I checked this</b>` |
| read, live | `Read {taken_at:%d %b %Y at %H:%M} UTC` + `, directly from {host}.` / ` by the Scout Sprite in a quarantined browser: {timings}.` / ` through Exa, after the site refused a direct fetch.` / ` through LlamaParse.` / `.` (no provenance) |
| read, archive | `From the Open Terms Archive: the version of {taken_at:%d %b %Y}.` / `From the Wayback Machine: the capture of {taken_at:%d %b %Y}.` |
| screenshot (only when `via` is not `scout` and there are Scout timings) | `Screenshot by the Scout Sprite: {timings}.` |
| timings | `network locked to 1 host (1.1 s), page rendered (8.4 s), rewound to its clean checkpoint (15.2 s)` (`hosts` plural when not 1; a missing time is left out) |
| judge | `Jev's answers (jev-1.14):` or `Jev's answers:` when no model is stored |
| answers | `• Would you want to know? yes, 91% sure` (`no, 80% sure` below 0.5) · `• About: arbitration` · `• Favours: the company` / `you` / `neither side` · `• How much it matters: Major (2.6 of 3)` · `• A way to opt out? yes, 88% sure` |
| partial (only when `change.partial`) | `• Large change: only part of it was judged.` |
| rule | `I alert when Jev is at least 70% sure a change matters.` (from `settings.material_threshold`) |
| unjudged (replaces judge, answers and rule when `change.material is None`) | `This change was not judged: no judge was configured.` |

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

`tests/test_inspector.py` (new). Build rows with direct SQL: an owner, members A and B, service `Acme` (`https://acme.test/terms`), A subscribed, two snapshots (the second `live`, `taken_at` `2026-11-28T08:14:00+00:00`, `sha` `"b" * 64`), and one change from the first to the second with `heading="3. Arbitration"`, `before_text`/`after_text` of a sentence each, `material=0.91`, `category="arbitration"`, `opt_out=0.88`, `severity=2.6`, `favours="company"`. Put that in a `seed(conn, provenance=None, evidence_path=None)` helper returning `(a, b, svc, change)`.

```python
DIRECT = {"via": "direct", "host": "acme.test", "judge_model": "jev-1.14",
          "scout": {"hosts": 1, "lock_ms": 1100, "run_ms": 8400, "restore_ms": 15200},
          "evidence": {"at": "2026-11-28T08:15:00+00:00", "png_sha256": "x", "text_match": False}}


def test_only_a_watcher_can_open_a_change(conn):
    a, b, svc, change = seed(conn)
    assert inspector.watched_change(conn, a, change.id)[1].id == svc.id
    assert inspector.watched_change(conn, b, change.id) is None
    assert inspector.watched_change(conn, a, 999) is None


def test_how_checked_for_a_direct_read_with_a_screenshot(conn):
    a, b, svc, change = seed(conn, provenance=DIRECT)
    assert inspector.how_checked(conn, change, svc).splitlines() == [
        "<b>🔬 How I checked this</b>",
        "Read 28 Nov 2026 at 08:14 UTC, directly from acme.test.",
        "Screenshot by the Scout Sprite: network locked to 1 host (1.1 s), page rendered (8.4 s), rewound to its clean checkpoint (15.2 s).",
        "Jev's answers (jev-1.14):",
        "• Would you want to know? yes, 91% sure",
        "• About: arbitration",
        "• Favours: the company",
        "• How much it matters: Major (2.6 of 3)",
        "• A way to opt out? yes, 88% sure",
        "I alert when Jev is at least 70% sure a change matters.",
    ]


def test_how_checked_without_provenance_and_from_the_archive(conn):
    a, b, svc, change = seed(conn)
    lines = inspector.how_checked(conn, change, svc).splitlines()
    assert lines[1] == "Read 28 Nov 2026 at 08:14 UTC." and lines[2] == "Jev's answers:"
    conn.execute("UPDATE snapshots SET origin = 'ota' WHERE id = ?", (change.to_snap,))
    assert inspector.how_checked(conn, change, svc).splitlines()[1] == "From the Open Terms Archive: the version of 28 Nov 2026."


def test_evidence_file_caption_and_the_missing_file(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(inspector, "settings", Settings(_env_file=None, data_dir=tmp_path))
    a, b, svc, change = seed(conn, provenance=DIRECT, evidence_path=f"1/terms/{'b' * 64}.png")
    assert inspector.evidence_file(conn, change, svc) is None        # path stored, file gone
    png = swarm.PNG_MAGIC + b"shot"
    path = tmp_path / "evidence" / "1" / "terms" / f"{'b' * 64}.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(png)
    name, data, caption = inspector.evidence_file(conn, change, svc)
    assert name == "evidence-acme-20261128.png" and data == png
    assert caption.splitlines() == [
        "📸 <b>Acme</b> as the Scout Sprite saw it on 28 Nov 2026 at 08:15 UTC.",
        f"Text SHA-256: <code>{'b' * 64}</code>",
        f"PNG SHA-256: <code>{hashlib.sha256(png).hexdigest()}</code>",
        "The page the Scout rendered differed from the text I judged; sites sometimes serve browsers differently.",
    ]
    conn.execute("UPDATE snapshots SET evidence_path = '../../fineprint.db' WHERE id = ?", (change.to_snap,))
    assert inspector.evidence_file(conn, change, svc) is None        # never outside the evidence folder


def test_diff_message_escapes_the_heading(conn):
    a, b, svc, change = seed(conn)
    conn.execute("UPDATE changes SET heading = 'Fees & <Charges>' WHERE id = ?", (change.id,))
    [fresh] = db.rows(db.Change, conn.execute("SELECT * FROM changes"))
    out = inspector.diff_message(conn, fresh, svc)
    assert out.startswith("<b>What changed in Acme · Fees &amp; &lt;Charges&gt;</b>\n<blockquote expandable>")
    assert out.endswith("<i><s>Struck through</s> was removed; <b>bold</b> was added.</i>")
```

`tests/test_bot.py`: reuse `seed` (import it from `tests.test_inspector`) with chats 1 (owner), 2 (A) and 3 (B) joined through `/start`.

```python
def tap(chat, data, callback_id="cb"):
    return Incoming(chat_id=chat, kind="tap", data=data, callback_id=callback_id, message_id=5)


async def test_a_watcher_opens_the_diff_and_a_stranger_gets_nothing(conn, tg):
    a, b, svc, change = seed(conn)
    await bot.handle(conn, None, tg, tap(a.chat_id, f"v1:cd:{change.id}"))
    assert tg.to(a.chat_id)[-1].startswith("<b>What changed in Acme")
    before = len(tg.sent)
    await bot.handle(conn, None, tg, tap(b.chat_id, f"v1:cd:{change.id}"))
    await bot.handle(conn, None, tg, tap(b.chat_id, f"v1:ch:{change.id}"))
    await bot.handle(conn, None, tg, tap(b.chat_id, f"v1:ce:{change.id}"))
    assert len(tg.sent) == before and tg.documents == []
    assert [c[2] for c in tg.called("answer")][-3:] == [bot.STALE_TAP] * 3


async def test_the_evidence_tap_says_when_the_screenshot_is_gone(conn, tg):
    a, b, svc, change = seed(conn, evidence_path=f"1/terms/{'b' * 64}.png")
    await bot.handle(conn, None, tg, tap(a.chat_id, f"v1:ce:{change.id}"))
    assert tg.documents == [] and tg.called("answer")[-1][2] == "That screenshot is no longer available."
```

`tests/test_agent.py`: one test that `agent.alert` orders its rows as inspector, opt-out, unwatch, and shows `📸 Evidence` only when `report.evidence` is true. `tests/test_e2e_smoke.py`: after the arbitration alert reaches member A, assert its first button row is `["🔍 What changed", "🔬 How I checked"]`, that A's `cd` tap is answered with the diff, and that B's is answered with `bot.STALE_TAP`.

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_inspector.py tests/test_watch.py -k "redline_html or inspector or how_checked or evidence or diff_message or watcher"`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.inspector'`).

- [ ] **Step 3: Implement**

`watch.redline_html`, the `agent` changes, `app/inspector.py`, and three `_dispatch` arms. Each arm calls `inspector.watched_change` first: on `None` it answers `STALE_TAP`; otherwise it answers the callback and sends (`ce` uses `tg.send_document(chat, name, png, caption=caption, mime="image/png")`). Existing tests that pinned the old button rows of an alert are updated to the new order.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/inspector.py app/watch.py app/agent.py app/bot.py tests/
git commit -m "Add inspector buttons: the diff, the evidence and how it was checked"
```

---

### Task 7: Open a past change

**Files:**
- Modify: `app/inspector.py` (new `full_card`, `change_list`), `app/bot.py` (`_dispatch`, `_watch`)
- Test: `tests/test_inspector.py`, `tests/test_bot.py`

**Interfaces:**
- Consumes: `inspector.watched_change`, `agent.report_for`, `agent.alert`, `agent.past_material_changes`.
- Produces:
  - `async inspector.full_card(conn, cl, user, change_id: int) -> Outgoing | None`: the change as `agent.alert(await agent.report_for(conn, cl, change, svc, user, {}, {}), svc)`; `None` for a non-watcher.
  - `inspector.change_list(conn, user, service_id: int) -> Outgoing | None`: `None` unless the member watches the service. Otherwise the newest 10 of `past_material_changes`, one button per row, label `{effective_at:%d %b} · {heading}` cut to 40 characters with `…`, data `co:<id>`. Text: `<b>Acme</b>: 3 material changes in the past year. Tap one to open it.` (singular `change`); with none: `No material changes to Acme in the past year.` and no buttons.
  - The `/watch` reply's buttons, when it has past changes: `[[("🔎 Open a change", cl:<service id>)], [unwatch]]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_inspector.py`:

```python
def test_change_list_shows_the_newest_ten_to_a_watcher_only(conn):
    a, b, svc, change = seed(conn)
    for n in range(11):
        conn.execute(
            "INSERT INTO changes (service_id, from_snap, to_snap, detected_at, effective_at, heading, kind, before_text,"
            " after_text, material) VALUES (?, ?, ?, ?, ?, ?, 'modified', 'x', 'y', 0.9)",
            (svc.id, change.from_snap, change.to_snap, db.now(), f"2026-10-{n + 1:02d}T00:00:00+00:00", f"Section {n} with a very long heading indeed"))
    out = inspector.change_list(conn, a, svc.id)
    assert out.text == "<b>Acme</b>: 12 material changes in the past year. Tap one to open it."
    assert len(out.buttons) == 10 and out.buttons[0][0][0] == "28 Nov · 3. Arbitration"
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

`tests/test_bot.py`: `co` by A sends a card whose text starts `<b>Acme</b> changed`; `co` and `cl` by B answer `bot.STALE_TAP` and send nothing; `cl` by A sends the list.

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

### Task 8: The redline kit narrates its three jobs

**Files:**
- Modify: `app/advocate.py` (`redline_kit`, `_passes`), `app/bot.py` (the `rt` arm of `_dispatch`)
- Test: `tests/test_advocate.py`, `tests/test_bot.py`

**Interfaces:**
- Consumes: `Swarm.run_job(on_stage=)` (Task 3), `progress.working`.
- Produces: `advocate.redline_kit(conn, cl, tg, user, ref, tone_code, on_stage: Stage = no_stage) -> str` and `_passes(cl, entry, tone, on_stage: Stage = no_stage)`; all three `run_job` calls receive `on_stage`.

- [ ] **Step 1: Write the failing tests**

`tests/test_advocate.py`: give `FakeSwarm.run_job` two more keyword arguments, `on_stage=None` and `trace=None`, and have it append `on_stage` to a new list `self.stage_callbacks`. Then, with the file's existing `kit_swarm` and `held` helpers:

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

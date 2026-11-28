# Lane 2: Wrapped Card and Two Engine Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `/wrapped` into a shareable image card, route Jev through TypeSafe's own connector when it is configured, and stop the year-of-history lookup missing services whose names have a capital inside.

**Architecture:** The card is drawn on the Gateway with Pillow from numbers computed in plain SQL, in three named looks so the owner can pick one from real renders. The two engine fixes are independent of the card and of each other; each is off or harmless until its setting or its input appears.

**Tech Stack:** Python 3.12, Pillow (new), SQLite, Pydantic, httpx, respx, pytest (asyncio auto mode).

**Spec:** `docs/superpowers/specs/2026-11-28-show-the-work-and-census-design.md`, Lane 2 and sections 3.7 and 3.8.

## Global Constraints

- All of `docs/superpowers/plans/2026-11-28-show-the-work-and-census.md` "Global Constraints" apply.
- Lane 0 is already on your branch: `Clients.judge`, `judge.active_model`, `settings.typesafe*`, and `FakeTelegram.send_photo` and `watch()` in `tests/test_bot.py`.
- The card shows numbers from the database and fixed copy only. No model output is drawn on it.
- CI installs with `uv sync --locked`: add Pillow with `uv add pillow` and commit `pyproject.toml` and `uv.lock` together.
- Fonts in the repository must be under the SIL Open Font License, with the licence text beside them.
- With `FP_TYPESAFE_BASE` unset, every Jev request is byte-for-byte what it is today.

## Review Focus

1. **A member who watches nothing asks for `/wrapped`.** Expected: one helpful sentence and no card. (Task 3)
2. **Rendering fails on the Sprite** (a missing font, a Pillow error). Expected: the written dissent still arrives. (Task 3)
3. **Text that does not fit the card:** a 300-character service name, a seven-digit word count. Expected: it is cut or wrapped, never drawn off the edge, and never raises. (Task 2)
4. **GitHub refuses the archive listing** (the unauthenticated limit is 60 calls an hour). Expected: the lookup proceeds with the name as given, and the failure is not remembered for a day. (Task 5)
5. **TypeSafe is configured but a call site still uses the chat gateway.** Expected: a test that fails if any Jev request reaches OpenCode. (Task 4)

---

### Task 1: The year's numbers

**Files:**
- Create: `app/wrapped.py`, `tests/test_wrapped.py`

**Interfaces:**
- Consumes: `agent.services_for`, `agent.documents_for`, `agent.latest_text`, `settings.material_threshold`.
- Produces:
  - `wrapped.WPM = 238`.
  - `wrapped.WrappedStats(BaseModel)`: `year: int`, `services: int = 0`, `documents: int = 0`, `words: int = 0`, `hours: float = 0.0`, `changes: int = 0`, `top: list[tuple[str, int]] = []`, `deadlines: int = 0`, `harshest: str = ""`, `comparison: str = ""`.
  - `wrapped.stats(conn, user: User, today: date | None = None) -> WrappedStats`.

Rules: `words` is the whitespace-separated word count of the current version of every document of every service the member watches. `hours` is `round(words / 238 / 60, 1)`. `changes` counts changes on those services with `effective_at` in the 365 days up to `today` that are material (`material >= settings.material_threshold` or `partial`). `top` is the three most frequent categories among them, most frequent first, ties in alphabetical order, leaving out `other` and uncategorised. `deadlines` counts the member's ledger rows of kind `opt_out` created in the same 365 days. `harshest` is `"{service} · {heading}"` for the counted change with the highest severity (the newest on a tie; heading falls back to `its terms`). `comparison` is left empty here.

- [ ] **Step 1: Write the failing tests** in `tests/test_wrapped.py`

```python
from datetime import UTC, date, datetime, timedelta

import pytest

from app import agent, db, ledger, users, wrapped


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "w.db")


async def seeded(conn):
    a = users.join_member(conn, "join-code", chat_id=1, name="A")
    b = users.join_member(conn, "join-code", chat_id=2, name="B")
    ids = {}
    for name, words, watcher in (("Acme", 20_000, a), ("Beta", 8_560, a), ("Gamma", 5_000, b)):
        svc = agent.add_service(conn, name, f"https://{name.lower()}.test/terms")
        await agent.record_version(conn, None, svc, "word " * words, datetime.now(UTC), "live")  # a first version: no judging
        conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)", (watcher.id, svc.id, db.now()))
        ids[name] = svc.id
    return a, b, ids


def add_change(conn, service_id, heading, category, severity, material=0.9, days_ago=10):
    snap = conn.execute("SELECT id FROM snapshots WHERE service_id = ?", (service_id,)).fetchone()["id"]
    at = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    conn.execute(
        "INSERT INTO changes (service_id, from_snap, to_snap, detected_at, effective_at, heading, kind, before_text,"
        " after_text, material, category, severity) VALUES (?, ?, ?, ?, ?, ?, 'modified', 'x', 'y', ?, ?, ?)",
        (service_id, snap, snap, at, at, heading, material, category, severity))


async def test_stats_count_only_the_members_own_year(conn):
    a, b, ids = await seeded(conn)
    for heading, category, severity in (("Disputes", "arbitration", 2.8), ("Class actions", "arbitration", 2.0), ("Your data", "data", 2.4)):
        add_change(conn, ids["Acme"], heading, category, severity)
    add_change(conn, ids["Beta"], "Typos", "other", 0.2)                   # counted, but never a top category
    add_change(conn, ids["Beta"], "Old", "fees", 3.0, days_ago=400)        # more than a year ago
    add_change(conn, ids["Beta"], "Cosmetic", "fees", 3.0, material=0.2)   # not material
    add_change(conn, ids["Gamma"], "Theirs", "renewal", 3.0)               # B's service
    ledger.add(conn, a.id, "Opt out: Acme", date.today() + timedelta(days=20), "opt_out")
    s = wrapped.stats(conn, a)
    assert (s.year, s.services, s.documents, s.words, s.hours) == (date.today().year, 2, 2, 28_560, 2.0)
    assert (s.changes, s.top, s.deadlines) == (4, [("arbitration", 2), ("data", 1)], 1)
    assert (s.harshest, s.comparison) == ("Acme · Disputes", "")


async def test_stats_for_a_member_who_watches_nothing(conn):
    a, b, ids = await seeded(conn)
    c = users.join_member(conn, "join-code", chat_id=3, name="C")
    assert wrapped.stats(conn, c) == wrapped.WrappedStats(year=date.today().year)
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_wrapped.py`
Expected: FAIL (`ImportError: cannot import name 'wrapped' from 'app'`).

- [ ] **Step 3: Implement `app/wrapped.py`** as specified in Interfaces and Rules.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/wrapped.py tests/test_wrapped.py
git commit -m "Compute a member's year in fine print from stored data"
```

---

### Task 2: The card, in three looks

**Files:**
- Create: `app/card.py`, `app/assets/fonts/` (font files and `OFL.txt`), `scripts/wrapped_mockups.py`, `tests/test_card.py`
- Modify: `pyproject.toml`, `uv.lock` (`uv add pillow`)

**Interfaces:**
- Consumes: `wrapped.WrappedStats` (Task 1).
- Produces:
  - `card.SIZE = (1080, 1350)`, `card.STYLES = ("docket", "poster", "receipt")`, `card.DEFAULT_STYLE = "docket"`, `card.DISSENT = "I respectfully dissent."`, `card.FOOTER = "Fine Print · information, not legal advice"`.
  - `card.lines(stats: WrappedStats) -> list[str]`: the sentences every look draws, in order.
  - `card.render(stats: WrappedStats, style: str = DEFAULT_STYLE) -> bytes`: PNG bytes; `ValueError` for an unknown style.
  - `scripts/wrapped_mockups.py <out_dir>`: writes `wrapped-docket.png`, `wrapped-poster.png` and `wrapped-receipt.png` from the sample numbers in the test below.

`lines` rules:

| Line | Text |
|---|---|
| words | `You agreed to 412,000 words across 14 services.` (`1 service`) |
| hours | `Reading them would take 29 hours.` (10 or more: a whole number; between 1 and 10: one decimal, `2.5 hours`; exactly 1: `1 hour`; under 1: `under an hour`) |
| changes | `37 changes that mattered · 9 arbitration · 6 data · 5 renewal` (`1 change that mattered`; with none: `Nothing that mattered changed this year.`) |
| deadlines | `4 opt-out deadlines caught` (`1 opt-out deadline caught`; left out when 0) |
| comparison | `stats.comparison`, left out when empty |

Every look also draws the title `AGREED WRAPPED {year}`, `DISSENT` and `FOOTER`. The three looks:

- **docket:** cream paper, a serif headline, the feel of a court filing: a caption block ("In the matter of your terms of service"), ruled lines, the dissent set like a signature.
- **poster:** a dark background with the word count as one very large number, bold sans-serif.
- **receipt:** a white till receipt on a coloured background, monospaced, each line an item with dotted leaders, the dissent where the total would be.

Fonts: one display serif, one sans-serif in two weights and one monospace, all OFL, as static `.ttf` files of at most 300 KB each (suggested families: DM Serif Display, IBM Plex Sans, IBM Plex Mono, from `github.com/google/fonts`). If they cannot be fetched, stop and report; do not ship Pillow's bitmap default.

- [ ] **Step 1: Write the failing tests** in `tests/test_card.py`

```python
import io

import pytest
from PIL import Image

from app import card
from app.wrapped import WrappedStats

SAMPLE = WrappedStats(year=2026, services=14, documents=31, words=412_000, hours=28.9, changes=37,
                      top=[("arbitration", 9), ("data", 6), ("renewal", 5)], deadlines=4, harshest="Spotify · Dispute resolution")


def test_lines_say_the_year_in_plain_sentences():
    assert card.lines(SAMPLE) == [
        "You agreed to 412,000 words across 14 services.",
        "Reading them would take 29 hours.",
        "37 changes that mattered · 9 arbitration · 6 data · 5 renewal",
        "4 opt-out deadlines caught",
    ]


def test_lines_for_a_quiet_or_tiny_year():
    quiet = WrappedStats(year=2026, services=1, documents=1, words=900, hours=0.1)
    assert card.lines(quiet) == [
        "You agreed to 900 words across 1 service.",
        "Reading them would take under an hour.",
        "Nothing that mattered changed this year.",
    ]
    one = WrappedStats(year=2026, services=2, documents=2, words=14_280, hours=1.0, changes=1, top=[("fees", 1)], deadlines=1)
    assert card.lines(one)[1:] == ["Reading them would take 1 hour.", "1 change that mattered · 1 fees", "1 opt-out deadline caught"]
    assert card.lines(one.model_copy(update={"hours": 2.5}))[1] == "Reading them would take 2.5 hours."


@pytest.mark.parametrize("style", card.STYLES)
def test_every_look_renders_the_same_portrait_png_each_time(style):
    png = card.render(SAMPLE, style)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and Image.open(io.BytesIO(png)).size == (1080, 1350)
    assert len(png) < 2_000_000 and card.render(SAMPLE, style) == png


def test_long_text_is_drawn_and_unknown_looks_are_refused():
    long = SAMPLE.model_copy(update={
        "words": 9_876_543, "harshest": "A" * 300,
        "comparison": "Your services force arbitration more often than most: 79% against 61%."})
    for style in card.STYLES:
        assert card.render(long, style)[:4] == b"\x89PNG"
    with pytest.raises(ValueError):
        card.render(SAMPLE, "neon")
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_card.py`
Expected: FAIL (`ModuleNotFoundError: No module named 'PIL'`).

- [ ] **Step 3: Implement**

`uv add pillow`; add the fonts and `OFL.txt`; write `app/card.py` and the mockup script. Load fonts by path relative to `card.py`. Wrap or shrink text to the card's width by measuring it (`ImageDraw.textlength`), and cut a line that still does not fit with `…`.

- [ ] **Step 4: Render the three looks and look at them**

Run: `uv run python scripts/wrapped_mockups.py /tmp/wrapped-mockups` and open the three files.
Expected: nothing clipped or overlapping; each look clearly different from the others. Report the three paths to the orchestrator so Dhananjay can pick one; `DEFAULT_STYLE` stays `docket` until he does.

- [ ] **Step 5: Run the whole suite and commit**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`

```bash
git add app/card.py app/assets/fonts scripts/wrapped_mockups.py tests/test_card.py pyproject.toml uv.lock
git commit -m "Draw the Agreed Wrapped card in three looks"
```

---

### Task 3: `/wrapped` sends the card

**Files:**
- Modify: `app/telegram.py` (new `Telegram.send_photo`), `app/bot.py` (the `/wrapped` branch of `handle`)
- Test: `tests/test_telegram.py`, `tests/test_bot.py`

**Interfaces:**
- Consumes: `wrapped.stats` (Task 1), `card.render` (Task 2), `agent.wrapped` (unchanged).
- Produces: `async Telegram.send_photo(chat_id, filename: str, content: bytes, *, caption: str = "") -> int | None`: `sendPhoto` as multipart with the bytes in the `photo` field and an HTML caption cut to `CAPTION_MAX`, counted and reported exactly like `send_document`.

Behaviour of `/wrapped`: when the member watches nothing, reply `You're not watching anything yet, so there is no year to wrap up. Try /watch spotify.` and stop. Otherwise send the card (filename `agreed-wrapped-{year}.png`, caption `<b>Agreed Wrapped {year}</b>`), then the written dissent exactly as today. If rendering or sending the card raises, log the exception's type and send the text alone.

- [ ] **Step 1: Write the failing tests**

`tests/test_telegram.py`, modelled on `test_send_document_uploads_multipart_from_memory`:

```python
@respx.mock
async def test_send_photo_uploads_multipart_with_an_html_caption():
    route = respx.post(f"{API}/botT/sendPhoto").mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 7}}))
    tg = Telegram("T")
    assert await tg.send_photo(42, "card.png", b"\x89PNG-bytes", caption="<b>Agreed Wrapped 2026</b>") == 7
    body = route.calls[0].request.content
    assert b'name="photo"; filename="card.png"' in body and b"\x89PNG-bytes" in body
    assert b"<b>Agreed Wrapped 2026</b>" in body and b"HTML" in body
    await tg.aclose()
```

`tests/test_bot.py` (add `from datetime import date`, `from app import card`, `from app.outgoing import Outgoing`):

```python
async def dissent(conn_, cl_, user):
    return Outgoing(text="<b>Agreed Wrapped</b>\nI dissent.")


async def test_wrapped_sends_the_card_then_the_text(conn, tg, monkeypatch):
    await say(conn, tg, 1, "/start owner-code")
    watch(conn, users.owner(conn))
    monkeypatch.setattr(agent, "wrapped", dissent)
    await say(conn, tg, 1, "/wrapped")
    [photo] = tg.photos
    year = date.today().year
    assert photo["content"][:4] == b"\x89PNG" and photo["filename"] == f"agreed-wrapped-{year}.png"
    assert photo["caption"] == f"<b>Agreed Wrapped {year}</b>" and tg.to(1)[-1] == "<b>Agreed Wrapped</b>\nI dissent."


async def test_wrapped_falls_back_to_the_text_when_the_card_fails(conn, tg, monkeypatch):
    await say(conn, tg, 1, "/start owner-code")
    watch(conn, users.owner(conn))
    monkeypatch.setattr(agent, "wrapped", dissent)

    def broken(stats, style=card.DEFAULT_STYLE):
        raise OSError("cannot open font")

    monkeypatch.setattr(card, "render", broken)
    await say(conn, tg, 1, "/wrapped")
    assert tg.photos == [] and tg.to(1)[-1] == "<b>Agreed Wrapped</b>\nI dissent."


async def test_wrapped_with_an_empty_watchlist_says_so(conn, tg):
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 1, "/wrapped")
    assert tg.photos == [] and tg.to(1)[-1] == "You're not watching anything yet, so there is no year to wrap up. Try /watch spotify."
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_bot.py -k wrapped tests/test_telegram.py::test_send_photo_uploads_multipart_with_an_html_caption`
Expected: FAIL (`AttributeError: 'Telegram' object has no attribute 'send_photo'`; `tg.photos` is empty).

- [ ] **Step 3: Implement** `Telegram.send_photo` and the `/wrapped` branch. Compute the stats and render inside the existing progress bubble; send the photo and the text after the bubble closes.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/telegram.py app/bot.py tests/test_telegram.py tests/test_bot.py
git commit -m "Send the Agreed Wrapped card before the written dissent"
```

---

### Task 4: Jev through the TypeSafeAI connector

**Files:**
- Modify: `app/judge.py` (every request to System One), and every caller of an `app.judge` function in `app/agent.py`, `app/ask.py`, `app/ask_tools.py`, `app/advocate.py` and `app/playbooks.py`
- Test: `tests/test_judge.py`, `tests/test_agent.py`

**Interfaces:**
- Consumes: `Clients.judge`, `judge.active_model`, `settings.typesafe_model` (Lane 0).
- Produces:
  - In `app/judge.py`: `SYSTEMONE_PATHS = {"opencode": "systemone/v1/systemone", "typesafe": "v1/systemone"}`; every request uses the path for `gw.provider.name` (any other name uses the OpenCode path) and `"model": active_model(gw)`.
  - `judge.red_flags` (the whole-document call) sends the header `TypeSafe-Danger-Zone-Long-Context: true` when, and only when, `gw.provider.name == "typesafe"`.
  - Every call into `app.judge` passes the judge gateway (`cl.judge`), not `cl.ai`. Calls into `app.writer`, `app.llm` and the `/ask` agents are the chat model and keep `cl.ai`. Where code reads the gateway with `getattr(cl, "ai", None)` for a Jev call (as `advocate._passes` does for `judge.redline_delta`), use `getattr(cl, "judge", None) or getattr(cl, "ai", None)`, so test doubles without a `judge` attribute still work.

The model id and header come from the day-one client (`git show adf4ec7:app/judge.py`); the owner confirms both against TypeSafe's API once the connector exists, by setting `FP_TYPESAFE_MODEL`.

- [ ] **Step 1: Write the failing tests**

`tests/test_agent.py`:

```python
TS_JEV = "https://typesafe.test/v1/systemone"


@respx.mock
async def test_jev_goes_to_typesafe_and_never_to_opencode_when_configured(conn, members, history, monkeypatch):
    custom = Settings(_env_file=None, typesafe_base="https://typesafe.test", typesafe_key="k", typesafe_model="jev-latest")
    monkeypatch.setattr(agent, "settings", custom)
    monkeypatch.setattr(agent.judge, "settings", custom)
    m = mock_providers()
    direct = respx.post(TS_JEV).mock(return_value=httpx.Response(200, json=GOOD))
    cl = agent.Clients()
    _svc, reports = await agent.subscribe(conn, cl, members[0], "Acme", "https://acme.test/terms")
    await cl.aclose()
    assert reports and direct.call_count == 4 and not m["jev"].called
    first = direct.calls[0].request
    assert json.loads(first.content)["model"] == "jev-latest" and first.headers["authorization"] == "Bearer k"
    assert m["chat"].called  # writing stays on the chat model
```

`tests/test_judge.py`:

```python
@respx.mock
async def test_only_whole_document_calls_to_typesafe_ask_for_long_context():
    ts = Gateway(Provider(name="typesafe", base_url="https://typesafe.test", key="k"))
    oc = Gateway(Provider(name="opencode", base_url="https://opencode.test/inference", key="k"))
    answers = {"model": "jev", "answers": {"arbitration": {"type": "noul", "noul": 0.9}}}
    direct = respx.post("https://typesafe.test/v1/systemone").mock(return_value=httpx.Response(200, json=answers))
    via_opencode = respx.post("https://opencode.test/inference/systemone/v1/systemone").mock(return_value=httpx.Response(200, json=answers))
    flags = {"arbitration": "Does the document require arbitration?"}
    assert await judge.red_flags(ts, "Terms", "All disputes go to arbitration.", flags) == {"arbitration": 0.9}
    await judge.nouls(ts, {"clause": "x"}, {"arbitration": "Is it about arbitration?"})
    await judge.red_flags(oc, "Terms", "All disputes go to arbitration.", flags)
    assert direct.calls[0].request.headers["typesafe-danger-zone-long-context"] == "true"
    assert "typesafe-danger-zone-long-context" not in direct.calls[1].request.headers
    assert "typesafe-danger-zone-long-context" not in via_opencode.calls[0].request.headers
    await ts.aclose()
    await oc.aclose()
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_agent.py::test_jev_goes_to_typesafe_and_never_to_opencode_when_configured tests/test_judge.py::test_only_whole_document_calls_to_typesafe_ask_for_long_context`
Expected: FAIL (the TypeSafe route is never called; Jev still posts to OpenCode).

- [ ] **Step 3: Implement**

Change `app/judge.py` first, then the callers. Find them with `grep -rn "judge\.[a-z_]*(" app/ | grep -v "^app/judge.py"` and check each one's first argument. Keep guards such as `if cl.ai:` that decide whether a judge exists at all, but test `cl.judge` where the guard protects only a Jev call.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass with no other test changed: with TypeSafe unset, `cl.judge is cl.ai`.

- [ ] **Step 5: Commit**

```bash
git add app/judge.py app/agent.py app/ask.py app/ask_tools.py app/advocate.py app/playbooks.py tests/test_judge.py tests/test_agent.py
git commit -m "Send Jev requests through TypeSafe's own connector when configured"
```

(Add only the files you changed.)

---

### Task 5: Archive folders matched without regard to case

**Files:**
- Modify: `app/history.py` (`ota_versions`; new `ota_folder`)
- Create: `tests/test_history.py`

**Interfaces:**
- Produces:
  - `history.FOLDER_TTL_S = 24 * 3600`, `history.FAILED_TTL_S = 600`, and the cache `history._FOLDERS: dict[str, tuple[float, dict[str, str]]]` (repo to expiry time and a map of case-folded folder name to its real name).
  - `async history.ota_folder(client: httpx.AsyncClient, repo: str, service: str) -> str`: lists the repo's top level with `GET https://api.github.com/repos/{repo}/git/trees/HEAD`, keeps entries of type `tree`, and returns the one equal to `service` ignoring case, or `service` itself when there is none. A successful listing is reused for `FOLDER_TTL_S`; a failed one (any status but 200, or an HTTP error) counts as an empty listing for `FAILED_TTL_S` only.
  - `ota_versions` asks `ota_folder` for each repo and uses its answer in the document path.

Background: the archive's folders are `YouTube` and `LinkedIn`, while Fine Print names those services `Youtube` and `Linkedin`, so today they silently fall back to eight Wayback captures (checked live on 28 Nov 2026: 3 commits for `YouTube/Terms of Service.md`, 0 for `Youtube/…`).

- [ ] **Step 1: Write the failing tests** in `tests/test_history.py`

```python
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app import history

PGA = "https://api.github.com/repos/OpenTermsArchive/pga-versions"
CONTRIB = "https://api.github.com/repos/OpenTermsArchive/contrib-versions"
SINCE = datetime(2025, 11, 28, tzinfo=UTC)
COMMIT = [{"sha": "abc", "commit": {"committer": {"date": "2026-03-01T00:00:00Z"}}}]


@pytest.fixture(autouse=True)
def fresh_cache():
    history._FOLDERS.clear()
    yield
    history._FOLDERS.clear()


@respx.mock
async def test_the_archive_folder_is_matched_without_regard_to_case():
    tree = respx.get(f"{PGA}/git/trees/HEAD").mock(return_value=httpx.Response(200, json={"tree": [
        {"path": "YouTube", "type": "tree"}, {"path": "README.md", "type": "blob"}]}))
    commits = respx.get(f"{PGA}/commits").mock(return_value=httpx.Response(200, json=COMMIT))
    raw = respx.get("https://raw.githubusercontent.com/OpenTermsArchive/pga-versions/abc/YouTube/Terms%20of%20Service.md").mock(
        return_value=httpx.Response(200, text="## Terms\nBe nice."))
    async with httpx.AsyncClient() as client:
        versions = await history.ota_versions(client, "Youtube", SINCE)
        await history.ota_versions(client, "youtube", SINCE)
    assert [v.origin for v in versions] == ["ota"] and raw.called
    assert commits.calls[0].request.url.params["path"] == "YouTube/Terms of Service.md"
    assert tree.call_count == 1  # listed once, then remembered


@respx.mock
async def test_a_refused_listing_uses_the_name_as_given_and_is_soon_retried(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(history.time, "monotonic", lambda: clock["now"])
    tree = respx.get(f"{PGA}/git/trees/HEAD").mock(return_value=httpx.Response(403, json={"message": "rate limit exceeded"}))
    respx.get(f"{CONTRIB}/git/trees/HEAD").mock(return_value=httpx.Response(403, json={}))
    commits = respx.get(f"{PGA}/commits").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{CONTRIB}/commits").mock(return_value=httpx.Response(200, json=[]))
    async with httpx.AsyncClient() as client:
        assert await history.ota_versions(client, "Youtube", SINCE) == []
        assert commits.calls[0].request.url.params["path"] == "Youtube/Terms of Service.md"
        await history.ota_versions(client, "Youtube", SINCE)
        assert tree.call_count == 1                      # not asked again straight away
        clock["now"] += history.FAILED_TTL_S + 1
        await history.ota_versions(client, "Youtube", SINCE)
        assert tree.call_count == 2                      # but not forgotten for a day either
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_history.py`
Expected: FAIL (`AttributeError: module 'app.history' has no attribute '_FOLDERS'`).

- [ ] **Step 3: Implement in `app/history.py`** as specified (`import time` at module level so the test can patch the clock).

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/history.py tests/test_history.py
git commit -m "Find archived terms for services whose names have a capital inside"
```

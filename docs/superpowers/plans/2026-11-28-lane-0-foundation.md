# Lane 0: Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the shared pieces every other lane builds on, so no two lanes race for a schema version, a button name or a test helper.

**Architecture:** Three small, independent additions: schema v8, the full button grammar for the change, and one setting plus the test fakes every lane shares. Nothing here changes what a member sees.

**Tech Stack:** Python 3.12, SQLite, Pydantic, pydantic-settings, pytest.

**Spec:** `docs/superpowers/specs/2026-11-28-show-the-work-and-census-design.md` (Lane 1 "One migration for the whole change", sections 1.5, 3.4, 3.6, 4.2).

## Global Constraints

- All of `docs/superpowers/plans/2026-11-28-show-the-work-and-census.md` "Global Constraints" apply.
- Work on the branch `show-the-work-and-census`. Do not push or open a pull request; the
  orchestrator does that.
- With every new setting unset, behaviour is exactly today's. No existing test may change meaning.
- Run one test with `uv run pytest -q tests/<file>::<name>`; the suite with `uv run pytest -q`; lint
  with `uvx ruff@0.17.6 check .`.

## Review Focus

1. **A database from before this change with real rows.** Expected: it upgrades in place, keeps
   every snapshot, and leaves a `.v7.bak` copy. (Task 1)
2. **Button data a client forged for the new actions:** letters where an id belongs, an unknown
   flag, a too-short reference. Expected: `taps.parse` returns `None`. (Task 2)
3. **A shared fake drifting from the real client.** Expected: `FakeTelegram.send_document` and
   `send_photo` take the same arguments the real `Telegram` methods take. (Task 3)

---

### Task 1: Schema v8

**Files:**
- Modify: `app/db.py` (`SCHEMA_VERSION`, a new `V8_MIGRATION`, `MIGRATIONS`, `Snapshot`, a new `CensusRow`)
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `snapshots.provenance TEXT` (NULL for old rows); table `census`; `db.Snapshot.provenance: str | None = None`; `db.CensusRow` with fields `domain: str`, `role: Literal["terms", "privacy"]`, `flag: str`, `name: str`, `url: str`, `p: float`, `clause: str | None = None`, `severity: float | None = None`, `has_opt_out: bool = False`, `sha: str`, `graded_at: datetime`.

- [ ] **Step 1: Write the failing tests** in `tests/test_db.py` (add `import pytest` at the top)

```python
CENSUS_INSERT = "INSERT INTO census (domain, role, flag, name, url, p, sha, graded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"


def test_fresh_database_has_provenance_and_census(tmp_path):
    conn = db.connect(tmp_path / "v8.db")
    assert db.SCHEMA_VERSION == 8
    assert "provenance" in {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")}
    conn.execute(CENSUS_INSERT, ("spotify.com", "terms", "arbitration", "Spotify", "https://www.spotify.com/legal/", 0.93, "a" * 64, db.now()))
    [row] = db.rows(db.CensusRow, conn.execute("SELECT * FROM census"))
    assert (row.clause, row.severity, row.has_opt_out) == (None, None, False)
    with pytest.raises(sqlite3.IntegrityError):  # one row per (domain, role, flag)
        conn.execute(CENSUS_INSERT, ("spotify.com", "terms", "arbitration", "S", "u", 0.1, "b" * 64, db.now()))


def test_v7_database_upgrades_with_backup_and_keeps_snapshots(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "SCHEMA_VERSION", 7)
    conn = db.connect(path)
    conn.execute("INSERT INTO services (name, terms_url, domain, added_at) VALUES ('Acme', 'https://acme.test/terms', 'acme.test', ?)", (db.now(),))
    conn.execute("INSERT INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (1, ?, 'live', 'abc', 'hello')", (db.now(),))
    conn.close()
    monkeypatch.undo()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert db.backup_path(path, 7).exists()
    [snap] = db.rows(db.Snapshot, conn.execute("SELECT * FROM snapshots"))
    assert snap.text == "hello" and snap.provenance is None
```

If the v7 `snapshots` table needs more columns than this INSERT gives, add them to the INSERT; do not weaken the assertions.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest -q tests/test_db.py -k "provenance or v7_database"`
Expected: FAIL (`SCHEMA_VERSION == 8` is false; no table `census`).

- [ ] **Step 3: Implement in `app/db.py`**

Set `SCHEMA_VERSION = 8`, add `8: V8_MIGRATION` to `MIGRATIONS`, add the two model changes above, and add this migration (statements are split on `";\n"`, so keep that separator and no trailing semicolon):

```python
# Version 8: how each live version was read (provenance), and the census of popular services'
# standing terms: one row per (registrable domain, document role, red flag), found or not.
V8_MIGRATION = """
ALTER TABLE snapshots ADD COLUMN provenance TEXT;
CREATE TABLE census (
    domain      TEXT NOT NULL,
    role        TEXT NOT NULL,
    flag        TEXT NOT NULL,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    p           REAL NOT NULL,
    clause      TEXT,
    severity    REAL,
    has_opt_out INTEGER NOT NULL DEFAULT 0,
    sha         TEXT NOT NULL,
    graded_at   TEXT NOT NULL,
    PRIMARY KEY (domain, role, flag)
)
"""
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass. A test that pinned version 7 by number is updated to 8, not deleted.

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_db.py
git commit -m "Add schema v8: snapshot provenance and the census table"
```

---

### Task 2: The button grammar for the whole change

**Files:**
- Modify: `app/taps.py` (module docstring grammar, `HELP_TOPICS`, `SLOW`, `METRIC`, `_valid`)
- Test: `tests/test_taps.py`

**Interfaces:**
- Produces, for the other lanes to build and handle:

| Data | Meaning | Argument |
|---|---|---|
| `v1:cd:<change id>` | show the diff | digits, at most 12 |
| `v1:ce:<change id>` | send the evidence screenshot | digits, at most 12 |
| `v1:ch:<change id>` | how it was checked | digits, at most 12 |
| `v1:co:<change id>` | open the change as a full card | digits, at most 12 |
| `v1:cl:<service id>` | list a service's material changes | digits, at most 12 |
| `v1:wa:<ref>` | yes, watch all (RAM-only offer) | matches `taps.SECRET` |
| `v1:rf:<flag key>` | a census flag's ranking | a key of `FLAG_EXPLANATIONS` |
| `v1:h:demo` | see a real alert | (help topic) |

- `taps.SLOW` gains `"wa"` only. `taps.METRIC` gains `cd: inspect_diff`, `ce: inspect_evidence`, `ch: inspect_how`, `co: open_change`, `cl: list_changes`, `wa: watch_many`, `rf: radar_flag`.

- [ ] **Step 1: Write the failing tests** in `tests/test_taps.py`

```python
REF = "A" * 22


@pytest.mark.parametrize("action,arg,extra", [
    ("cd", "7", ""), ("ce", "7", ""), ("ch", "7", ""), ("co", "123456789012", ""), ("cl", "3", ""),
    ("wa", REF, ""), ("rf", "arbitration", ""), ("rf", "ai_model_training", ""), ("h", "demo", ""),
])
def test_new_actions_round_trip(action, arg, extra):
    data = taps.build(action, arg, extra)
    assert len(data.encode()) <= 64 and taps.parse(data) == Tap(action, arg, extra)


@pytest.mark.parametrize("data", [
    "v1:cd:abc", "v1:ce:-1", "v1:ch:1234567890123", "v1:co:7:x", "v1:cl:", "v1:wa:short", f"v1:wa:{REF}:x",
    "v1:rf:not_a_flag", "v1:rf:arbitration:x", "v1:h:secrets",
])
def test_forged_new_actions_are_refused(data):
    assert taps.parse(data) is None


def test_inspector_taps_are_quick_and_watch_many_is_slow():
    assert taps.is_slow(f"v1:wa:{REF}")
    assert not any(taps.is_slow(d) for d in ("v1:cd:7", "v1:ce:7", "v1:ch:7", "v1:co:7", "v1:cl:3", "v1:rf:arbitration", "v1:h:demo"))


def test_every_new_action_has_a_metric_name():
    assert {a: taps.METRIC[a] for a in ("cd", "ce", "ch", "co", "cl", "wa", "rf")} == {
        "cd": "inspect_diff", "ce": "inspect_evidence", "ch": "inspect_how", "co": "open_change",
        "cl": "list_changes", "wa": "watch_many", "rf": "radar_flag"}
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest -q tests/test_taps.py -k "new_actions or forged or inspector or metric_name"`
Expected: FAIL (`ValueError: invalid button data for action 'cd'`).

- [ ] **Step 3: Implement in `app/taps.py`**

Add the eight lines to the grammar in the module docstring, `"demo"` to `HELP_TOPICS`, `"wa"` to `SLOW`, the seven `METRIC` entries, and three `case` arms in `_valid`: `"cd" | "ce" | "ch" | "co" | "cl"` validated exactly like `"oc"`; `"wa"` like `"od"`; `"rf"` like `"fx"`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/taps.py tests/test_taps.py
git commit -m "Define the button grammar for the inspector, census and showcase"
```

---

### Task 3: The showcase setting and shared test fakes

**Files:**
- Modify: `app/config.py` (`Settings`), `.env.example`, `tests/test_bot.py` (`FakeTelegram`, new `watch`)
- Create: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `settings.showcase_service: str = ""` (environment variable `FP_SHOWCASE_SERVICE`): the display name of the demo company. Empty means every feature that depends on it stays hidden.
  - `tests.test_bot.FakeTelegram.send_document(chat, filename, content, *, caption="", mime="application/octet-stream") -> int` recording into `self.documents`, and `send_photo(chat, filename, content, *, caption="") -> int` recording into `self.photos`; each record is a dict with keys `chat`, `filename`, `content`, `caption` (and `mime` for documents).
  - `tests.test_bot.watch(conn, user, name="Acme", url="https://acme.test/terms") -> Service`: a module-level test helper that calls `agent.add_service(conn, name, url)`, inserts the member's subscription (`INSERT OR IGNORE INTO subscriptions (user_id, service_id, created_at)`), and returns the service. Every lane's bot tests use it.

No provider, client or model setting is added: Jev stays on OpenCode through `Clients.ai`, exactly as today.

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py` (new):

```python
from app.config import Settings


def test_the_showcase_service_is_off_by_default_and_read_from_settings():
    assert Settings(_env_file=None).showcase_service == ""
    assert Settings(_env_file=None, showcase_service="Gobblr").showcase_service == "Gobblr"
```

`tests/test_bot.py`:

```python
async def test_the_shared_fakes_record_files_and_watches(conn, tg):
    member = users.join_member(conn, "join-code", chat_id=5, name="M")
    svc = watch(conn, member)
    assert watch(conn, member).id == svc.id  # watching twice changes nothing
    assert [s.name for s in agent.services_for(conn, member.id)] == ["Acme"]
    assert await tg.send_document(5, "a.png", b"x", caption="c", mime="image/png")
    assert await tg.send_photo(5, "b.png", b"y", caption="d")
    assert tg.documents == [{"chat": 5, "filename": "a.png", "content": b"x", "caption": "c", "mime": "image/png"}]
    assert tg.photos == [{"chat": 5, "filename": "b.png", "content": b"y", "caption": "d"}]
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest -q tests/test_config.py tests/test_bot.py::test_the_shared_fakes_record_files_and_watches`
Expected: FAIL (`AttributeError: 'Settings' object has no attribute 'showcase_service'`; `NameError: name 'watch' is not defined`).

- [ ] **Step 3: Implement**

- `app/config.py`: the `showcase_service` field, commented as "the demo company's display name; shows members a 'See a real alert' button".
- `.env.example`: under Tuning add `FP_SHOWCASE_SERVICE=` with a one-line comment in the file's existing style.
- `tests/test_bot.py`: the two `FakeTelegram` recorders and the `watch` helper.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/config.py .env.example tests/test_config.py tests/test_bot.py
git commit -m "Add the showcase setting and shared test fakes"
```

# Lane 3: Census Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Grade the standing terms of about 80 popular services with Jev, publish their clauses to Pinecone so the 📊 benchmark line appears, and show the results through `/radar` and a faster, friendlier `/watch`.

**Architecture:** A census is graded, not watched: it has its own table and never creates snapshots or history. Grading asks only Jev (flags, then which section states each flag, then how much that clause matters); the clause is always a section of the stored text. Pages come from one Apify crawl when that connector exists, otherwise from the existing fetch ladder with the Scout skipped.

**Tech Stack:** Python 3.12, SQLite, Pydantic, httpx, respx, pytest (asyncio auto mode); TypeSafe's Jev; Pinecone; Apify.

**Spec:** `docs/superpowers/specs/2026-11-28-show-the-work-and-census-design.md`, Lane 3 (sections 3.1 to 3.6).

## Global Constraints

- All of `docs/superpowers/plans/2026-11-28-show-the-work-and-census.md` "Global Constraints" apply.
- Lane 0 is already on your branch: the `census` table and `db.CensusRow`, the button data `wa`, `rf` and `cl`, `Clients.judge`, `Clients.apify`, and `FakeTelegram`'s recorders and `watch()` in `tests/test_bot.py`.
- Every Jev call takes the judge gateway (`cl.judge`). The census never calls a chat model.
- The census never writes to `services`, `snapshots` or `changes`, and never uses the Scout.
- `app/agent.py` must not import `app/census.py` (the census imports the agent).
- `services.domain` holds a host, not a registrable domain. Match a service to the census with `census.site(svc.terms_url)`, never with a SQL join on `domain`.
- A census row's `domain` is the registrable domain of the seed's **terms** URL, for both of its documents.
- Nothing is published to Pinecone for a document that failed its sanity check.

## Review Focus

1. **A page that is not the terms** (a cookie wall, a login page, a soft 404). Expected: reported as failed, no rows saved, nothing published. (Task 5)
2. **`/census run` sent twice.** Expected: one run; the second message says one is already going. (Task 5)
3. **Names with `<` or `&` in census output.** Expected: escaped everywhere. (Task 6)
4. **The "Yes, watch all" offer after a restart, a Sprite sleep or a second tap.** Expected: "That offer has expired. Send me the names again." (Task 8)
5. **`/radar` before any census has run, or for an unknown service.** Expected: one plain sentence. (Task 6)

---

### Task 1: The seed list

**Files:**
- Create: `app/census_seed.py`, `tests/test_census_seed.py`

**Interfaces:**
- Produces: `census_seed.Seed(NamedTuple)` with `name: str`, `terms: str`, `privacy: str = ""`; and `census_seed.SEEDS: tuple[Seed, ...]`.

Choose between 70 and 100 well-known consumer services across streaming, social, shopping, travel and rides, productivity, AI assistants, finance, gaming and messaging, each with the URL of its terms of service and, where you know it, its privacy policy. Include `Fly.io`, `TypeSafe`, `Box`, `Pinecone`, `Ollama` and `Apify`. A wrong URL is caught at run time (Task 5), so prefer the canonical English page.

- [ ] **Step 1: Write the failing test** in `tests/test_census_seed.py`

```python
from urllib.parse import urlparse

from app.census_seed import SEEDS
from app.discover import registrable_domain


def test_the_seed_list_is_usable():
    assert 70 <= len(SEEDS) <= 100
    names = [s.name.casefold() for s in SEEDS]
    assert len(set(names)) == len(names)
    assert {"fly.io", "typesafe", "box", "pinecone", "ollama", "apify"} <= set(names)
    for s in SEEDS:
        for url in filter(None, (s.terms, s.privacy)):
            assert url.startswith("https://") and "." in (urlparse(url).hostname or "")
    sites = [registrable_domain(urlparse(s.terms).hostname) for s in SEEDS]
    assert len(set(sites)) == len(sites)  # the census keys a service by its terms page's registrable domain
```

- [ ] **Step 2: Run and see it fail**

Run: `uv run pytest -q tests/test_census_seed.py`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.census_seed'`).

- [ ] **Step 3: Write `app/census_seed.py`.** Two services whose terms share a registrable domain (two products of one company) count as one: keep the better known.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/census_seed.py tests/test_census_seed.py
git commit -m "List the popular services the census grades"
```

---

### Task 2: Grading one document with Jev

**Files:**
- Create: `app/census.py`, `tests/test_census.py`

**Interfaces:**
- Consumes: `judge.red_flags`, `judge.nouls`, `judge.clause_severity`, `playbooks.battery`, `watch.sections`.
- Produces:
  - Constants: `FOUND = 0.7`, `ROLE_DOMAIN = {"terms": "consumer_tos", "privacy": "privacy_and_ai"}`, `WINDOW_CHARS = judge.MAX_DOCUMENT_CHARS`, `WINDOW_OVERLAP = 5_000`, `MAX_CANDIDATES = 3`, `CLAUSE_CHARS = 2_000`, `STATE_BUDGET_CHARS = 60_000`, and `FLAG_WORDS: dict[str, frozenset[str]]` with an entry for every flag of both batteries.
  - `census.FlagGrade(BaseModel)`: `flag: str`, `p: float`, `clause: str | None = None`, `severity: float | None = None`, `has_opt_out: bool = False`.
  - `census.windows(text: str) -> list[str]`: `[text]` when it fits in `WINDOW_CHARS`; otherwise slices of `WINDOW_CHARS` starting every `WINDOW_CHARS - WINDOW_OVERLAP` characters until the end is covered.
  - `census.candidates(text: str, flag: str) -> list[str]`: up to `MAX_CANDIDATES` units of the text that share at least one word with `FLAG_WORDS[flag]`, most shared words first (document order on a tie), each cut to `CLAUSE_CHARS`. A unit is a `watch.sections` section rendered with its `.text`; when the text has fewer than three sections, units are its paragraphs (split on blank lines).
  - `async census.grade(gw: Gateway | None, name: str, role: str, text: str) -> list[FlagGrade]`: one grade per flag of `playbooks.battery(ROLE_DOMAIN[role])`, in battery order.

How `grade` works:

1. For each window, `judge.red_flags(gw, title, window, battery)` with title `"{name} ({DOC_ROLE_LABELS[role]})"`; keep the highest answer per flag.
2. For each flag with `p >= FOUND`, take its candidates. In **one** `judge.nouls(gw, state, questions)` call: `state` maps `"{flag}__{n}"` (n from 1) to the candidate text; for each there are two questions, keyed `"{flag}__{n}"` ("Does `{flag}__{n}` state this? " followed by the battery's question for the flag) and `"opt__{flag}__{n}"` ("Does `{flag}__{n}` give the reader a way to opt out of it, reject it or object to it?"). If the candidate texts total more than `STATE_BUDGET_CHARS`, drop every third candidate, then every second.
3. A flag's clause is its candidate with the highest answer, if that answer is at least `FOUND`; `has_opt_out` is whether that candidate's `opt__` answer is at least `FOUND`. With no such candidate the flag stays found with `clause=None`.
4. One `judge.clause_severity(gw, clauses)` call scores the located clauses.

- [ ] **Step 1: Write the failing tests** in `tests/test_census.py`

```python
import pytest

from app import agent, census, db, playbooks

DOC = "\n".join([
    "## 1. Your account", "You must keep your password safe.",
    "## 2. Fees", "Subscriptions renew automatically each month until you cancel.",
    "## 3. Disputes", "All disputes are resolved by binding arbitration and you waive any class action.",
])


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "census.db")


def test_the_census_uses_the_same_bar_as_verdict_cards():
    assert census.FOUND == agent.FOUND
    both = set(playbooks.battery("consumer_tos")) | set(playbooks.battery("privacy_and_ai"))
    assert both <= set(census.FLAG_WORDS)


def test_windows_cover_a_long_document_with_overlap():
    assert census.windows("short") == ["short"]
    parts = census.windows("x" * 200_000)
    assert [len(p) for p in parts] == [90_000, 90_000, 30_000]


def test_candidates_are_the_sections_that_use_the_flags_words():
    assert census.candidates(DOC, "arbitration")[0].startswith("## 3. Disputes")
    assert census.candidates(DOC, "auto_renews")[0].startswith("## 2. Fees")
    assert census.candidates(DOC, "content_licence") == []
    flat = "Welcome to Acme.\n\nAll disputes go to binding arbitration.\n\nThanks for reading."
    assert census.candidates(flat, "arbitration") == ["All disputes go to binding arbitration."]


def fake_jev(monkeypatch, flags, sections, severity=2.4):
    async def red_flags(gw, title, text, battery):
        return {k: flags.get(k, 0.1) for k in battery}

    async def nouls(gw, state, questions):
        return {k: sections.get(k, 0.0) for k in questions}

    async def clause_severity(gw, clauses):
        return [severity for _ in clauses]

    monkeypatch.setattr(census.judge, "red_flags", red_flags)
    monkeypatch.setattr(census.judge, "nouls", nouls)
    monkeypatch.setattr(census.judge, "clause_severity", clause_severity)


async def test_grade_locates_scores_and_notes_the_opt_out(monkeypatch):
    fake_jev(monkeypatch, {"arbitration": 0.9, "auto_renews": 0.9},
             {"arbitration__1": 0.95, "opt__arbitration__1": 0.8, "auto_renews__1": 0.4})
    grades = {g.flag: g for g in await census.grade(None, "Acme", "terms", DOC)}
    assert list(grades) == list(playbooks.battery("consumer_tos"))
    arbitration = grades["arbitration"]
    assert (arbitration.p, arbitration.severity, arbitration.has_opt_out) == (0.9, 2.4, True)
    assert arbitration.clause.startswith("## 3. Disputes")
    assert grades["auto_renews"] == census.FlagGrade(flag="auto_renews", p=0.9)   # found, but no section confirmed
    assert grades["hard_to_cancel"] == census.FlagGrade(flag="hard_to_cancel", p=0.1)


async def test_grade_keeps_the_highest_answer_across_windows(monkeypatch):
    fake_jev(monkeypatch, {}, {})
    monkeypatch.setattr(census, "windows", lambda text: ["first", "second"])

    async def red_flags(gw, title, text, battery):
        return {k: (0.9 if (k, text) == ("arbitration", "second") else 0.2) for k in battery}

    monkeypatch.setattr(census.judge, "red_flags", red_flags)
    grades = {g.flag: g.p for g in await census.grade(None, "Acme", "terms", DOC)}
    assert grades["arbitration"] == 0.9 and grades["auto_renews"] == 0.2


async def test_grade_uses_the_privacy_battery_for_a_privacy_policy(monkeypatch):
    fake_jev(monkeypatch, {}, {})
    grades = await census.grade(None, "Acme", "privacy", DOC)
    assert [g.flag for g in grades] == list(playbooks.battery("privacy_and_ai"))
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_census.py`
Expected: FAIL (`ImportError: cannot import name 'census' from 'app'`).

- [ ] **Step 3: Implement `app/census.py`** as specified. `FLAG_WORDS` holds six to ten lower-case words per flag (for example `arbitration`: arbitration, arbitrate, arbitrator, class, waive, waiver, dispute, disputes).

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/census.py tests/test_census.py
git commit -m "Grade a document's standing clauses with Jev alone"
```

---

### Task 3: Storing and reading the census

**Files:**
- Modify: `app/census.py`
- Test: `tests/test_census.py`

**Interfaces:**
- Consumes: `db.CensusRow`, `census_seed.Seed`, `discover.registrable_domain`.
- Produces:
  - `census.FLAG_LABELS: dict[str, str]`, exactly: `auto_renews: "auto-renews"`, `arbitration: "binding arbitration"`, `data_sharing: "shares or sells your data"`, `unilateral_changes: "can change the terms without asking"`, `content_licence: "broad licence to your content"`, `hard_to_cancel: "hard to cancel"`, `ai_model_training: "can train AI on your data"`, `data_broker_sale: "sells data to brokers"`, `biometric_or_location: "collects biometrics or location"`, `indefinite_retention: "keeps data indefinitely"`, `cross_context_ads: "tracks you across sites"`, `no_deletion_right: "no right to deletion"`.
  - `census.site(url_or_host: str) -> str`: the registrable domain of a URL or a bare host.
  - `census.save(conn, seed: Seed, role: str, url: str, sha: str, grades: list[FlagGrade], at: datetime | None = None) -> None`: in one transaction, replaces every row for `(site(seed.terms), role)`.
  - `census.graded_sha(conn, domain: str, role: str) -> str | None`.
  - `census.drop(conn, domain: str) -> list[CensusRow]`: deletes that domain's rows and returns them.
  - `census.Status(BaseModel)`: `services: int`, `documents: int`, `graded_at: datetime | None`; `census.status(conn) -> Status` (`graded_at` is the newest).
  - `census.Share(BaseModel)`: `flag: str`, `found: int`, `of: int`, and a property `pct -> int` (`round(100 * found / of)`); `census.shares(conn) -> list[Share]`: one per flag that has rows, counting rows with `p >= FOUND` over all rows of that flag, highest percentage first, then flag name.
  - `census.ranked(conn, flag: str, n: int = 5) -> tuple[list[CensusRow], list[CensusRow]]`: among rows of that flag with `p >= FOUND` and both `clause` and `severity` set: the `n` most severe (most severe first), and the `n` least severe (least severe first), by `severity` then `name`.
  - `census.standing(conn, domain: str) -> list[CensusRow]`: that domain's rows with `p >= FOUND`, terms before privacy, each in battery order.
  - `census.standing_line(conn, domain: str) -> str | None`: `None` when the domain has no rows; otherwise the found **terms** flags as `🔴 {label}` joined with ` · `, or `🟢 none of the red flags I check for` when there are none, followed by ` (census, {graded_at:%d %b})`.

- [ ] **Step 1: Write the failing tests** in `tests/test_census.py` (add `from datetime import UTC, datetime`, `from app.census import FlagGrade`, `from app.census_seed import Seed`)

```python
AT = datetime(2026, 11, 29, 6, 0, tzinfo=UTC)


def put(conn, name, arbitration=None, renews=0.1, role="terms"):
    """Save one document: `arbitration` is None (not found) or its severity (found, located and scored)."""
    grades = [FlagGrade(flag="auto_renews", p=renews)]
    grades.append(FlagGrade(flag="arbitration", p=0.1) if arbitration is None else
                  FlagGrade(flag="arbitration", p=0.9, clause=f"{name} arbitrates.", severity=arbitration))
    seed = Seed(name, f"https://www.{name.lower()}.test/terms")
    census.save(conn, seed, role, seed.terms, "s" * 64, grades, at=AT)


def test_site_is_the_registrable_domain():
    assert census.site("https://policies.google.com/terms") == "google.com"
    assert census.site("www.bbc.co.uk") == "bbc.co.uk"


def test_saving_replaces_a_documents_rows_and_shares_add_up(conn):
    for name, severity in (("Acme", 2.8), ("Beta", 1.2), ("Gamma", 2.0), ("Delta", None)):
        put(conn, name, severity)
    put(conn, "Acme", 2.9)  # graded again: replaced, not added
    assert conn.execute("SELECT COUNT(*) FROM census").fetchone()[0] == 8
    assert census.graded_sha(conn, "acme.test", "terms") == "s" * 64 and census.graded_sha(conn, "nope.test", "terms") is None
    assert census.status(conn) == census.Status(services=4, documents=4, graded_at=AT)
    assert [(s.flag, s.found, s.of, s.pct) for s in census.shares(conn)] == [("arbitration", 3, 4, 75), ("auto_renews", 0, 4, 0)]


def test_ranked_lists_only_located_and_scored_clauses(conn):
    for name, severity in (("Acme", 2.8), ("Beta", 1.2), ("Gamma", 2.0)):
        put(conn, name, severity)
    seed = Seed("Vague", "https://vague.test/terms")
    census.save(conn, seed, "terms", seed.terms, "s" * 64, [FlagGrade(flag="arbitration", p=0.9)], at=AT)  # found, not located
    harshest, gentlest = census.ranked(conn, "arbitration", n=2)
    assert [r.name for r in harshest] == ["Acme", "Gamma"] and [r.name for r in gentlest] == ["Beta", "Gamma"]


def test_standing_line(conn):
    put(conn, "Acme", 2.8, renews=0.9)
    put(conn, "Mild")
    assert census.standing_line(conn, "acme.test") == "🔴 auto-renews · 🔴 binding arbitration (census, 29 Nov)"
    assert census.standing_line(conn, "mild.test") == "🟢 none of the red flags I check for (census, 29 Nov)"
    assert census.standing_line(conn, "unknown.test") is None


def test_drop_returns_what_it_removed(conn):
    put(conn, "Acme", 2.8)
    assert {r.flag for r in census.drop(conn, "acme.test")} == {"auto_renews", "arbitration"}
    assert census.drop(conn, "acme.test") == [] and census.status(conn).services == 0
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_census.py -k "site or saving or ranked or standing or drop"`
Expected: FAIL (`AttributeError: module 'app.census' has no attribute 'save'`).

- [ ] **Step 3: Implement** the functions above in `app/census.py`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/census.py tests/test_census.py
git commit -m "Store the census and read shares, rankings and a service's standing"
```

---

### Task 4: Publishing clauses, and a benchmark line that uses them

**Files:**
- Modify: `app/memory.py` (`ClauseMetadata`, `remember`), `app/benchmark.py` (`TOP_K`, `Benchmark`, `from_matches`, `benchmark_clause`), `app/agent.py` (`_change_benchmark`), `app/census.py` (new `vector_id`, `publish`, `unpublish`)
- Test: `tests/test_benchmark.py`, `tests/test_census.py`

**Interfaces:**
- Produces:
  - `memory.ClauseMetadata.site: str = ""`; `memory.remember(..., site: str = "", public: bool = True)` (both keyword-only, both stored in the vector's metadata).
  - `benchmark.TOP_K = 200`.
  - `benchmark.Benchmark.gentler: list[str]` **replaces** `comparable_services`: up to three services of the cohort whose severity is strictly below this clause's, gentlest first. `Benchmark.line(category)` appends ` Gentler: A, B, C.` when the list is not empty.
  - `benchmark.from_matches(matches, severity, exclude_service=None, exclude_site=None)` and `benchmark.benchmark_clause(gw, text, category, severity, exclude_service=None, *, exclude_site=None, vector=None)`: a match is left out when its `service` equals `exclude_service` or its non-empty `site` equals `exclude_site`.
  - `agent._change_benchmark` passes `exclude_site=discover.registrable_domain(<host of svc.terms_url>)`.
  - `census.vector_id(domain, role, flag) -> str`: `f"census-{domain}-{role}-{flag}"`.
  - `async census.publish(gw: Gateway, name: str, domain: str, role: str, grades: list[FlagGrade]) -> int`: for each grade with a clause, a severity and a `playbooks.FLAG_CATEGORY` entry, one `memory.remember` with id `vector_id(...)`, `service=name`, `heading=FLAG_LABELS[flag]`, the clause, that category, `domain=ROLE_DOMAIN[role]`, `severity`, `scale=benchmark.SCALE`, `has_opt_out`, `site=domain`. Returns how many were upserted.
  - `async census.unpublish(gw: Gateway, rows: list[CensusRow]) -> int`: the same upsert for each located row with `public=False`, so benchmarks' `public: true` filter leaves it out.

- [ ] **Step 1: Write the failing tests**

`tests/test_benchmark.py`: change the existing `test_benchmark_over_a_full_cohort` to expect `gentler=["S0", "S1", "S2"]` and the line `More severe than 67% of comparable arbitration clauses across 12 public services. Gentler: S0, S1, S2.`, then add:

```python
def test_a_services_own_census_clause_is_left_out_by_site():
    own = memory.Match.model_validate({"id": "census-spotify.com-terms-arbitration", "score": 0.99, "metadata": {
        "service": "Spotify AB", "site": "spotify.com", "category": "arbitration", "severity": 3.0, "scale": "clause", "public": True}})
    assert from_matches([own, *matches(10)], severity=2.0, exclude_service="Spotify", exclude_site="spotify.com").cohort_size == 10
    assert from_matches([own, *matches(10)], severity=2.0, exclude_service="Spotify").cohort_size == 11


def test_no_gentler_services_when_this_clause_is_the_gentlest():
    found = from_matches(matches(12), severity=0.0)
    assert found.gentler == [] and found.line("arbitration").endswith("across 12 public services.")


def test_a_cohort_can_cover_the_whole_census():
    assert benchmark.TOP_K >= 200
    assert from_matches(matches(120), severity=2.0).cohort_size == 120
```

`tests/test_census.py`:

```python
import json

import httpx
import respx

from app.config import Provider
from app.gateway import Gateway

EMBED = "https://pinecone.test/embed"
UPSERT = "https://pinecone.test/indexes/fine-print/vectors/upsert"


@respx.mock
async def test_publish_upserts_only_located_scored_clauses(conn):
    respx.post(EMBED).mock(return_value=httpx.Response(200, json={"data": [{"values": [0.1] * 1024}]}))
    upsert = respx.post(UPSERT).mock(return_value=httpx.Response(200, json={"upsertedCount": 1}))
    gw = Gateway(Provider(name="pinecone", base_url="https://pinecone.test", key="k", auth_header="Api-Key", auth_prefix=""))
    grades = [FlagGrade(flag="arbitration", p=0.9, clause="All disputes go to arbitration.", severity=2.4, has_opt_out=True),
              FlagGrade(flag="auto_renews", p=0.9), FlagGrade(flag="hard_to_cancel", p=0.1)]
    assert await census.publish(gw, "Acme", "acme.test", "terms", grades) == 1
    [vector] = json.loads(upsert.calls[0].request.content)["vectors"]
    assert vector["id"] == "census-acme.test-terms-arbitration"
    assert {k: vector["metadata"][k] for k in ("service", "site", "category", "scale", "severity", "has_opt_out", "public", "domain")} == {
        "service": "Acme", "site": "acme.test", "category": "arbitration", "scale": "clause", "severity": 2.4,
        "has_opt_out": True, "public": True, "domain": "consumer_tos"}
    put(conn, "Acme", 2.8)
    assert await census.unpublish(gw, census.drop(conn, "acme.test")) == 1
    assert json.loads(upsert.calls[1].request.content)["vectors"][0]["metadata"]["public"] is False
    await gw.aclose()
```

`tests/test_e2e_smoke.py`: extend the existing smoke test (or add one beside it, reusing its fakes) so that the Pinecone query for the arbitration change returns twelve census clauses from twelve different services, and assert the alert member A receives contains `📊 More severe than` and `Gentler:`.

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_benchmark.py tests/test_census.py -k "gentler or census_clause or cohort or publish"`
Expected: FAIL (`from_matches() got an unexpected keyword argument 'exclude_site'`; no attribute `publish`).

- [ ] **Step 3: Implement** the changes above. Update every other use of `comparable_services` in `app/` and `tests/`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/memory.py app/benchmark.py app/agent.py app/census.py tests/test_benchmark.py tests/test_census.py
git commit -m "Publish census clauses and name gentler services in the benchmark line"
```

---

### Task 5: Running the census, and the owner's commands

**Files:**
- Modify: `app/census.py` (new `looks_like`, `read_page`, `run`, `RunSummary`), `app/bot.py` (`OWNER_MENU`, `OWNER_HELP`, `handle`, new `_census`)
- Test: `tests/test_census.py`, `tests/test_bot.py`

**Interfaces:**
- Consumes: `grade` (Task 2), `save`, `graded_sha`, `drop`, `status` (Task 3), `publish`, `unpublish` (Task 4), `agent.page_text`, `discover.find_terms_url`, `watch.sha`, `watch.normalise`, `watch.markdown_to_canonical`, `sprite.stay_awake`.
- Produces:
  - `census.MAX_SERVICES = 100`, `CONCURRENCY = 4`, `PROGRESS_EVERY = 10`, `MIN_PAGE_CHARS = 2_000`.
  - `census.looks_like(role: str, text: str) -> bool`: at least `MIN_PAGE_CHARS` characters, and the word `privacy` (for `privacy`) or `terms` or `agreement` (for `terms`) at least three times, ignoring case.
  - `async census.read_page(cl, url: str) -> str`: `agent.page_text(cl, url, may_scout=lambda: False)`.
  - `census.RunSummary(BaseModel)`: `graded: int = 0`, `unchanged: int = 0`, `failed: list[str] = []`, `published: int = 0`.
  - `async census.run(conn, cl, seeds: Sequence[Seed], *, read: Callable[[list[str]], Awaitable[dict[str, str]]] | None = None, notify: Callable[[str], Awaitable[None]] | None = None) -> RunSummary`.

How `run` works: take the first `MAX_SERVICES` seeds. If `read` is given, call it once with every URL (terms and privacy) and treat its values as Markdown (`normalise(markdown_to_canonical(...))`); if it raises, carry on as if it returned nothing. Then, at most `CONCURRENCY` services at a time, for each document: use the bulk text if it passes `looks_like`, else `read_page`; if that still fails and the role is `terms`, try `discover.find_terms_url(seed.name, cl)` once and read that; if it still does not look right, add `"{name} ({role})"` to `failed` and move on. If `graded_sha` equals the text's `sha`, count it `unchanged`. Otherwise `grade(cl.judge, …)`, `save`, and, when `cl.pinecone` is set, `publish`. Any exception for one document adds it to `failed` and never stops the run. After every `PROGRESS_EVERY` services, `await notify(f"Census: {done} of {total} services done…")`.

Owner commands and exact replies:

| Message | Reply |
|---|---|
| `/census`, nothing graded | `Census: 82 services listed, none graded yet. Send /census run to start.` |
| `/census`, graded | `Census: 82 services listed, 61 graded (118 documents), last graded 29 Nov 2026.` |
| `/census run` | `Census started: 82 services. I'll report as it goes.` then progress lines, then `Census finished: 118 documents graded, 3 unchanged, 2 failed (Acme (terms), Beta (privacy)), 412 clauses published.` (no bracket when none failed) |
| `/census run` while one runs | `A census run is already going.` |
| the run itself raised | `Census stopped: RuntimeError.` |
| `/census drop spotify.com` | `Dropped spotify.com: 12 rows removed, 5 clauses withdrawn from benchmarks.` or `Nothing in the census for spotify.com.` |
| `/census anything-else` | `Usage: /census, /census run, or /census drop <domain>.` (escaped) |

The run happens in a background task that the bot keeps a reference to, inside `sprite.stay_awake("census", expire="10m")`, and reports to the owner's chat.

- [ ] **Step 1: Write the failing tests**

`tests/test_census.py` (add `from types import SimpleNamespace`):

```python
TERMS = "## Terms\n" + "These terms are an agreement between you and us. " * 60
PRIVACY = "## Privacy\n" + "This privacy policy explains what we collect. " * 60
CL = SimpleNamespace(judge=None, pinecone=object(), apify=None, ai=None, exa=None, llama=None, swarm=None)


def stub_grading(monkeypatch, published=None, broken=()):
    async def grade(gw, name, role, text):
        if name in broken:
            raise RuntimeError("jev is down")
        return [FlagGrade(flag="arbitration", p=0.9, clause="c", severity=2.0)]

    async def publish(gw, name, domain, role, grades):
        (published if published is not None else []).append((name, role))
        return 1

    monkeypatch.setattr(census, "grade", grade)
    monkeypatch.setattr(census, "publish", publish)


def test_looks_like_refuses_walls_and_wrong_pages():
    assert census.looks_like("terms", TERMS) and census.looks_like("privacy", PRIVACY)
    assert not census.looks_like("terms", "Please accept cookies to continue.")
    assert not census.looks_like("privacy", TERMS)   # long enough, but never says privacy


async def test_run_grades_saves_and_skips_what_has_not_changed(conn, monkeypatch):
    stub_grading(monkeypatch)
    seeds = (Seed("Acme", "https://acme.test/terms", "https://acme.test/privacy"), Seed("Beta", "https://beta.test/tos"))
    pages = {"https://acme.test/terms": TERMS, "https://acme.test/privacy": PRIVACY, "https://beta.test/tos": TERMS}
    asked = []

    async def read(urls):
        asked.append(sorted(urls))
        return pages

    first = await census.run(conn, CL, seeds, read=read)
    assert (first.graded, first.unchanged, first.failed, first.published) == (3, 0, [], 3)
    assert asked == [sorted(pages)] and census.status(conn).services == 2
    second = await census.run(conn, CL, seeds, read=read)
    assert (second.graded, second.unchanged, second.published) == (0, 3, 0)


async def test_a_page_that_is_not_the_terms_is_reported_and_never_published(conn, monkeypatch):
    published = []
    stub_grading(monkeypatch, published)

    async def wall(cl, url):
        return "Please accept cookies to continue."

    async def not_found(query, cl=None, client=None):
        return None

    monkeypatch.setattr(census, "read_page", wall)
    monkeypatch.setattr(census.discover, "find_terms_url", not_found)
    summary = await census.run(conn, CL, (Seed("Acme", "https://acme.test/terms"),))
    assert summary.failed == ["Acme (terms)"] and published == []
    assert conn.execute("SELECT COUNT(*) FROM census").fetchone()[0] == 0


async def test_one_bad_service_does_not_stop_the_rest_and_progress_is_reported(conn, monkeypatch):
    stub_grading(monkeypatch, broken=("Svc3",))
    seeds = tuple(Seed(f"Svc{n}", f"https://svc{n}.test/terms") for n in range(25))

    async def read(urls):
        return {u: TERMS for u in urls}

    said = []

    async def notify(text):
        said.append(text)

    summary = await census.run(conn, CL, seeds, read=read, notify=notify)
    assert (summary.graded, summary.failed) == (24, ["Svc3 (terms)"])
    assert said == ["Census: 10 of 25 services done…", "Census: 20 of 25 services done…"]


async def test_run_stops_at_one_hundred_services(conn, monkeypatch):
    stub_grading(monkeypatch)
    asked = []

    async def read(urls):
        asked.extend(urls)
        return {u: TERMS for u in urls}

    await census.run(conn, CL, tuple(Seed(f"S{n}", f"https://s{n}.test/terms") for n in range(120)), read=read)
    assert len(asked) == 100
```

`tests/test_bot.py` (add `import asyncio` and `from app import census`):

```python
async def test_census_is_owner_only_and_reports_its_status(conn, tg, monkeypatch):
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 2, "/census run")
    assert tg.to(2)[-1] == "That command is owner-only."
    monkeypatch.setattr(census, "SEEDS", tuple(census.Seed(f"S{n}", f"https://s{n}.test/t") for n in range(82)), raising=False)
    await say(conn, tg, 1, "/census")
    assert tg.to(1)[-1] == "Census: 82 services listed, none graded yet. Send /census run to start."


async def test_a_second_census_run_is_refused_while_one_is_going(conn, tg, monkeypatch):
    await say(conn, tg, 1, "/start owner-code")
    release = asyncio.Event()

    async def slow_run(conn_, cl_, seeds, read=None, notify=None):
        await release.wait()
        return census.RunSummary(graded=118, unchanged=3, failed=["Acme (terms)", "Beta (privacy)"], published=412)

    monkeypatch.setattr(census, "run", slow_run)
    await say(conn, tg, 1, "/census run")
    await say(conn, tg, 1, "/census run")
    assert tg.to(1)[-1] == "A census run is already going."
    release.set()
    await bot.census_task
    assert tg.to(1)[-1] == ("Census finished: 118 documents graded, 3 unchanged, 2 failed "
                            "(Acme (terms), Beta (privacy)), 412 clauses published.")
```

Make the seed list reachable for the first test as `census.SEEDS` (re-exported from `census_seed`), and expose the running task as `bot.census_task`. Add a test for `/census drop` (rows removed, the reply above) and for the usage reply, in the same style.

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_census.py -k "looks_like or run_ or page_that or bad_service" tests/test_bot.py -k census`
Expected: FAIL (`AttributeError: module 'app.census' has no attribute 'run'`).

- [ ] **Step 3: Implement** `looks_like`, `read_page`, `run` and the bot's `/census` handling. `/census run` passes `read=None` for now; Task 7 wires Apify in.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/census.py app/bot.py tests/test_census.py tests/test_bot.py
git commit -m "Run the census from Telegram and let the owner drop a wrong grade"
```

---

### Task 6: `/radar`

**Files:**
- Modify: `app/census.py` (new `radar`, `flag_card`, `service_card`), `app/bot.py` (`MEMBER_MENU`, `HELP`, `handle`, `_dispatch`)
- Test: `tests/test_census.py`, `tests/test_bot.py`

**Interfaces:**
- Consumes: `status`, `shares`, `ranked`, `standing`, `FLAG_LABELS` (Task 3), `judge.severity_label`.
- Produces:
  - `census.radar(conn) -> Outgoing`.
  - `census.flag_card(conn, flag: str) -> str`.
  - `census.service_card(conn, query: str) -> Outgoing | None`: the service whose census name equals `query` ignoring case, or whose domain equals `site(query)`; `None` when there is none.

Exact copy (labels are `FLAG_LABELS` with the first letter capitalised; names escaped):

- `radar`, graded:

```
📡 <b>Fine Print census</b> · 82 services, graded 29 Nov 2026
What their terms and privacy policies say today:
• Can change the terms without asking: 91%
• Auto-renews: 74%
Tap a flag for the harshest and gentlest. /radar spotify shows one service.
```

  One line per `shares` entry with a percentage above zero, in that order, and one button per line, two to a row: the capitalised label, data `rf:<flag>`.
- `radar`, nothing graded: `The census hasn't been run yet.` with no buttons.
- `flag_card`: `<b>Binding arbitration</b> · 50 of 82 services`, then `Harshest: Uber (Major 2.9), Lyft (Major 2.7)`, then `Gentlest: Tidal (Minor 0.8), Qobuz (Minor 1.1)`, then `<i>Ranked by how much the clause matters to the person agreeing, as Jev scored it. Information, not legal advice.</i>`. With no located clause, the two ranking lines are replaced by `No clause was located precisely enough to rank.`
- `service_card`: `<b>Spotify</b> · census of 29 Nov 2026`; one line per standing flag, `🔴 Binding arbitration — Major (2.6/3)` (`— not scored` when it has no severity); when there are none, `🟢 None of the red flags I check for.`; then, when any flag has a clause, `<blockquote expandable><b>What the documents say</b>` with one line per clause as `<b>{Label}</b>: “{clause cut to 300 characters}”` and `</blockquote>`; then `<i>Information, not legal advice.</i>`. One button: `👀 Watch Spotify`, data `w:<domain>`.
- `/radar spotify` for a service not in the census: `Spotify isn't in the census. /radar shows what is.`

- [ ] **Step 1: Write the failing tests**

`tests/test_census.py`:

```python
def test_radar_lists_shares_with_a_button_each(conn):
    assert census.radar(conn).text == "The census hasn't been run yet." and census.radar(conn).buttons == []
    for name, severity in (("Acme", 2.8), ("Beta", 1.2), ("Gamma", 2.0), ("Delta", None)):
        put(conn, name, severity, renews=0.9 if name == "Acme" else 0.1)
    out = census.radar(conn)
    assert out.text.splitlines() == [
        "📡 <b>Fine Print census</b> · 4 services, graded 29 Nov 2026",
        "What their terms and privacy policies say today:",
        "• Binding arbitration: 75%",
        "• Auto-renews: 25%",
        "Tap a flag for the harshest and gentlest. /radar spotify shows one service.",
    ]
    assert out.buttons == [[("Binding arbitration", "v1:rf:arbitration"), ("Auto-renews", "v1:rf:auto_renews")]]


def test_flag_card_ranks_and_escapes_names(conn):
    for name, severity in (("Acme", 2.8), ("Beta", 1.2)):
        put(conn, name, severity)
    seed = Seed("A<b>&C", "https://odd.test/terms")
    census.save(conn, seed, "terms", seed.terms, "s" * 64, [FlagGrade(flag="arbitration", p=0.9, clause="c", severity=2.0)], at=AT)
    lines = census.flag_card(conn, "arbitration").splitlines()
    assert lines[0] == "<b>Binding arbitration</b> · 3 of 3 services"
    assert lines[1] == "Harshest: Acme (Major 2.8), A&lt;b&gt;&amp;C (Worth knowing 2.0), Beta (Minor 1.2)"
    assert census.flag_card(conn, "auto_renews").splitlines()[1] == "No clause was located precisely enough to rank."


def test_service_card_by_name_or_domain(conn):
    put(conn, "Acme", 2.8)
    card = census.service_card(conn, "ACME")
    assert card.text.splitlines()[:2] == ["<b>Acme</b> · census of 29 Nov 2026", "🔴 Binding arbitration — Major (2.8/3)"]
    assert "<b>Binding arbitration</b>: “Acme arbitrates.”" in card.text and card.text.endswith("<i>Information, not legal advice.</i>")
    assert card.buttons == [[("👀 Watch Acme", "v1:w:acme.test")]]
    assert census.service_card(conn, "https://www.acme.test/terms").text == card.text
    assert census.service_card(conn, "nobody") is None
```

`tests/test_bot.py`: `/radar` for a member returns the "hasn't been run yet" line; after `put`-style rows, a member's `rf:arbitration` tap sends the flag card; `/radar nobody` replies `Nobody isn't in the census. /radar shows what is.`

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_census.py -k "radar or flag_card or service_card"`
Expected: FAIL (`AttributeError: module 'app.census' has no attribute 'radar'`).

- [ ] **Step 3: Implement** the three functions, `("radar", "What popular services' terms say today")` in `MEMBER_MENU`, a `/radar` line in `HELP`, the command in `handle`, and the `rf` arm in `_dispatch`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/census.py app/bot.py tests/test_census.py tests/test_bot.py
git commit -m "Add /radar: what popular services' terms say today"
```

---

### Task 7: Reading the pages with Apify

**Files:**
- Create: `app/apify.py`, `tests/test_apify.py`
- Modify: `app/bot.py` (`/census run` passes the reader)

**Interfaces:**
- Consumes: `Clients.apify` (Lane 0), `Gateway.request`, `agent.normalise_url`.
- Produces:
  - `apify.ACTOR = "apify~website-content-crawler"`, `MAX_CHARGE_USD = 2`, `WAIT_S = 60`, `GIVE_UP_S = 1200`, `COST_SETTLE_S = 10`, `TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})`.
  - `async apify.crawl(gw: Gateway, urls: list[str]) -> dict[str, str]`: the requested URL mapped to the page's Markdown (its `text` when there is no Markdown), for the pages Apify returned; `{}` when the run did not succeed or `GIVE_UP_S` passed.
  - `/census run` calls `census.run(..., read=functools.partial(apify.crawl, cl.apify))` when `cl.apify` is set.

The calls, all relative to the gateway's base, each validated with a Pydantic model:

1. `POST v2/actors/apify~website-content-crawler/runs?maxTotalChargeUsd=2` with JSON `{"startUrls": [{"url": u}, …], "maxCrawlDepth": 0, "maxCrawlPages": len(urls)}`. The run is `data` in the reply: `id`, `status`, `defaultDatasetId`.
2. `GET v2/actor-runs/{id}?waitForFinish=60`, with a request timeout of `WAIT_S + 30`, until `data.status` is in `TERMINAL` or `GIVE_UP_S` has passed.
3. On `SUCCEEDED`: `GET v2/datasets/{defaultDatasetId}/items?format=json&clean=true`. Each item has `url`, optional `crawl.loadedUrl`, `markdown` and `text`. Match an item to a requested URL by `agent.normalise_url` of its `url`, then of its `crawl.loadedUrl`.
4. Sleep `COST_SETTLE_S`, fetch the run once more and `gw.report("usd", data.usageTotalUsd)` (the first figure after a run ends is preliminary).

- [ ] **Step 1: Write the failing tests** in `tests/test_apify.py`

```python
import json

import httpx
import pytest
import respx

from app import apify
from app.config import Provider
from app.gateway import Gateway

BASE = "https://apify.test"
RUNS = f"{BASE}/v2/actors/apify~website-content-crawler/runs"
RUN = f"{BASE}/v2/actor-runs/run1"
ITEMS = f"{BASE}/v2/datasets/ds1/items"


def run(status, usd=None):
    return httpx.Response(200, json={"data": {"id": "run1", "status": status, "defaultDatasetId": "ds1", "usageTotalUsd": usd}})


class Recorder:
    """What `Gateway` needs of a metrics recorder; keeps the amounts a provider reported."""

    def __init__(self):
        self.amounts = []

    def count(self, kind, name, outcome="ok", ms=0):
        pass

    def usage(self, provider, unit, amount):
        self.amounts.append((provider, unit, amount))


@pytest.fixture
def recorder():
    return Recorder()


@pytest.fixture
def gw(monkeypatch, recorder):
    monkeypatch.setattr(apify, "COST_SETTLE_S", 0)
    return Gateway(Provider(name="apify", base_url=BASE, key="tok"), recorder=recorder)


@respx.mock
async def test_crawl_starts_one_capped_run_waits_and_returns_markdown_by_url(gw, recorder):
    start = respx.post(RUNS).mock(return_value=run("READY"))
    poll = respx.get(RUN).mock(side_effect=[run("RUNNING"), run("SUCCEEDED"), run("SUCCEEDED", 0.31)])
    respx.get(ITEMS).mock(return_value=httpx.Response(200, json=[
        {"url": "https://acme.test/terms", "markdown": "## Terms\nBe nice."},
        {"url": "https://m.beta.test/tos", "crawl": {"loadedUrl": "https://www.beta.test/tos/"}, "text": "Beta terms", "markdown": ""},
    ]))
    urls = ["https://acme.test/terms", "https://beta.test/tos", "https://gone.test/terms"]
    pages = await apify.crawl(gw, urls)
    await gw.aclose()
    assert pages == {"https://acme.test/terms": "## Terms\nBe nice.", "https://beta.test/tos": "Beta terms"}
    request = start.calls[0].request
    assert request.url.params["maxTotalChargeUsd"] == "2" and request.headers["authorization"] == "Bearer tok"
    assert json.loads(request.content) == {"startUrls": [{"url": u} for u in urls], "maxCrawlDepth": 0, "maxCrawlPages": 3}
    assert poll.calls[0].request.url.params["waitForFinish"] == "60" and poll.call_count == 3
    assert recorder.amounts == [("apify", "usd", 0.31)]  # the settled figure, not the preliminary one


@respx.mock
async def test_a_failed_run_returns_nothing(gw):
    respx.post(RUNS).mock(return_value=run("READY"))
    respx.get(RUN).mock(return_value=run("FAILED"))
    items = respx.get(ITEMS).mock(return_value=httpx.Response(200, json=[]))
    assert await apify.crawl(gw, ["https://acme.test/terms"]) == {} and not items.called
    await gw.aclose()


@respx.mock
async def test_crawl_gives_up_on_a_run_that_never_ends(gw, monkeypatch):
    monkeypatch.setattr(apify, "GIVE_UP_S", 0)
    respx.post(RUNS).mock(return_value=run("READY"))
    respx.get(RUN).mock(return_value=run("RUNNING"))
    assert await apify.crawl(gw, ["https://acme.test/terms"]) == {}
    await gw.aclose()
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_apify.py`
Expected: FAIL (`ImportError: cannot import name 'apify' from 'app'`).

- [ ] **Step 3: Implement `app/apify.py`** and pass the reader in `/census run`. An HTTP error at any step is logged by type and returns `{}`: the census then reads those pages itself.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/apify.py app/bot.py tests/test_apify.py
git commit -m "Read the census pages with one Apify crawl"
```

---

### Task 8: The census on `/watch`, and watching several at once

**Files:**
- Modify: `app/bot.py` (`_watch`, `handle`, `_dispatch`; new `names_in`, `WATCH_OFFERS`, `_subscribe_by_name`, `_watch_many`)
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `census.standing_line`, `census.site` (Task 3), `advocate.EphemeralCache`, button data `wa` and `cl` (Lane 0), `agent.subscribe`, `find_terms_url`.
- Produces:
  - `bot.names_in(text: str) -> list[str]`: the names in a several-services message, or `[]`. A leading cue is required and removed, ignoring case: `/watch`, `watch`, `i use`, `i'm using` or `we use`. The rest is split on commas, semicolons, new lines, ` and ` and `&`; each piece is stripped of spaces and trailing punctuation and kept if it fully matches `taps.SERVICE_NAME` and is not in `NOT_SERVICES`; repeats (ignoring case) are dropped. The answer is `[]` unless two to eight names remain.
  - `bot.WATCH_OFFERS = advocate.EphemeralCache(600, 64 * 1024)`.
  - `async bot._subscribe_by_name(conn, cl, user, arg: str, on_stage) -> tuple[Service, list[ChangeReport], str] | None`: what `_watch` does today to turn a name or URL into a subscription (find the URL, name it, `agent.subscribe`), returning the service, its past reports and the URL; `None` when no terms page is found. `_watch` uses it.

Behaviour and exact copy:

- **Census line.** In `_watch`, once the URL is known and before subscribing, if `census.standing_line(conn, census.site(url))` is not `None`, send `<b>{Name}</b> today: {line}` and, on the next line, `Now reading a year of its past versions…`. The rest of `/watch` is unchanged.
- **Offer.** When `names_in` finds names (for `/watch …` and for a plain message), send `Watch these 3? <b>Spotify</b>, <b>Netflix</b>, <b>Uber</b>` with buttons `Yes, watch all` (`wa:<ref>`) and `No` (`n`). Names are shown with their first letter capitalised.
- **Yes, watch all.** Take the offer out of `WATCH_OFFERS`. If it is gone: `That offer has expired. Send me the names again.` Otherwise, inside one progress bubble, subscribe to each in turn with the stage `2 of 3: Netflix…`, then send one closing message:

```
Watching 3 services: <b>Spotify</b>, <b>Netflix</b>, <b>Uber</b>.
7 material changes in the past year. The ones that matter most:
• <b>Spotify</b> · Dispute resolution — Major
• <b>Uber</b> · Fees — Worth knowing
I couldn't find terms for: Zzyzx.
```

  The bullets are the three most severe past changes across all of them. With no changes at all the second line is `No material changes in the past year across them. I'll check daily.` and there are no bullets. The last line appears only for names that failed. Buttons: one row per service watched, `🔎 Spotify` with data `cl:<service id>`.

- [ ] **Step 1: Write the failing tests** in `tests/test_bot.py`

```python
@pytest.mark.parametrize("text,expected", [
    ("/watch spotify, netflix and uber", ["spotify", "netflix", "uber"]),
    ("I use Spotify, Netflix, Uber & Notion.", ["Spotify", "Netflix", "Uber", "Notion"]),
    ("we use slack;zoom\nnotion", ["slack", "zoom", "notion"]),
    ("watch notion", []),                                         # one name: the ordinary path
    ("spotify, netflix", []),                                     # no cue: not an order to watch
    ("I use thanks, ok", []),                                     # greetings are not services
    ("I use " + ", ".join(f"svc{n}" for n in range(9)), []),      # more than eight
    ("I use Spotify, spotify, Netflix", ["Spotify", "Netflix"]),
    ("I use Apple Music, Netflix, Uber", ["Netflix", "Uber"]),     # a name with a space cannot ride in a button
])
def test_names_in(text, expected):
    assert bot.names_in(text) == expected


async def test_several_names_are_offered_then_watched_in_turn(conn, tg, monkeypatch):
    await say(conn, tg, 1, "/start owner-code")
    seen = []

    async def find(query, cl=None, client=None):
        return None if query == "zzyzx" else f"https://{query}.test/terms"

    async def subscribe(conn_, cl_, user, name, url, on_stage=None):
        seen.append(name)
        return watch(conn, user, name, url), []

    monkeypatch.setattr(bot, "find_terms_url", find)
    monkeypatch.setattr(agent, "subscribe", subscribe)
    await say(conn, tg, 1, "I use spotify, netflix and zzyzx")
    offer = tg.last(1)
    assert offer["text"] == "Watch these 3? <b>Spotify</b>, <b>Netflix</b>, <b>Zzyzx</b>"
    yes = offer["buttons"][0][0][1]
    await bot.handle(conn, None, tg, Incoming(chat_id=1, kind="tap", data=yes, message_id=offer["id"]))
    assert seen == ["Spotify", "Netflix"]
    closing = tg.last(1)
    assert closing["text"].splitlines() == [
        "Watching 2 services: <b>Spotify</b>, <b>Netflix</b>.",
        "No material changes in the past year across them. I'll check daily.",
        "I couldn't find terms for: Zzyzx.",
    ]
    assert [row[0][0] for row in closing["buttons"]] == ["🔎 Spotify", "🔎 Netflix"]
    assert all(row[0][1].startswith("v1:cl:") for row in closing["buttons"])
    await bot.handle(conn, None, tg, Incoming(chat_id=1, kind="tap", data=yes, message_id=offer["id"]))
    assert tg.to(1)[-1] == "That offer has expired. Send me the names again."


async def test_watch_says_what_a_census_service_says_today(conn, tg, monkeypatch):
    await say(conn, tg, 1, "/start owner-code")
    seed = census.Seed("Acme", "https://acme.test/terms")
    census.save(conn, seed, "terms", seed.terms, "s" * 64, [census.FlagGrade(flag="arbitration", p=0.9)])

    async def subscribe(conn_, cl_, user, name, url, on_stage=None):
        return watch(conn, user, name, url), []

    monkeypatch.setattr(agent, "subscribe", subscribe)
    await say(conn, tg, 1, "/watch https://acme.test/terms")
    first = next(t for t in tg.to(1) if t.startswith("<b>Acme</b> today:"))
    assert "🔴 binding arbitration (census, " in first and first.endswith("Now reading a year of its past versions…")
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_bot.py -k "names_in or several_names or census_service"`
Expected: FAIL (`AttributeError: module 'app.bot' has no attribute 'names_in'`).

- [ ] **Step 3: Implement** in `app/bot.py`. In `handle`, look for several names before the single-name suggestion and before treating the text as a question.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/bot.py tests/test_bot.py
git commit -m "Show the census on /watch and watch several services in one message"
```

---

### Task 9: The comparison line on the Wrapped card

Do this only after Lane 2 has merged and your branch is rebased on it. It is the first thing to cut.

**Files:**
- Modify: `app/census.py` (new `comparison`, `FLAG_PHRASES`), `app/wrapped.py` (`stats`)
- Test: `tests/test_census.py`, `tests/test_wrapped.py`

**Interfaces:**
- Consumes: `wrapped.WrappedStats.comparison` and `card.lines` (Lane 2), `census.shares`, `census.site`.
- Produces:
  - `census.FLAG_PHRASES: dict[str, str]`: a verb phrase per flag that reads after "Your services" (`arbitration: "force arbitration"`, `auto_renews: "renew automatically"`, `data_sharing: "share or sell your data"`, `unilateral_changes: "let themselves change the terms"`, `content_licence: "take a broad licence to your content"`, `hard_to_cancel: "make cancelling hard"`, and one for each privacy flag in the same style).
  - `census.MIN_COMPARED = 5`, `census.MIN_GAP = 10`.
  - `census.comparison(conn, domains: Sequence[str]) -> str`: among the given domains that are in the census (at least `MIN_COMPARED`, else `""`), for each flag compare the share of them where it is found with the census-wide share; return `Your services {phrase} more often than most: {theirs}% against {census}%.` for the flag with the largest gap of at least `MIN_GAP` points, else `""`.
  - `wrapped.stats` sets `comparison` from the member's services (`census.site(svc.terms_url)` for each).

- [ ] **Step 1: Write the failing test** in `tests/test_census.py`

```python
def test_comparison_names_the_biggest_gap_or_stays_silent(conn):
    mine = [f"Mine{n}" for n in range(6)]
    for n, name in enumerate(mine):
        put(conn, name, 2.0 if n < 5 else None)        # arbitration in 5 of my 6
    for n in range(14):
        put(conn, f"Other{n}", 2.0 if n < 5 else None)  # and in 5 of the other 14: 10 of 20 overall
    domains = [f"{name.lower()}.test" for name in mine]
    assert census.comparison(conn, domains) == "Your services force arbitration more often than most: 83% against 50%."
    assert census.comparison(conn, domains[:4]) == ""                       # too few of mine are in the census
    assert census.comparison(conn, [f"other{n}.test" for n in range(14)]) == ""  # 36% against 50%: not more often
```

Add one test in `tests/test_wrapped.py` that `stats(...).comparison` is that sentence when the member watches those six services.

- [ ] **Step 2: Run and see it fail**

Run: `uv run pytest -q tests/test_census.py::test_comparison_names_the_biggest_gap_or_stays_silent`
Expected: FAIL (`AttributeError: module 'app.census' has no attribute 'comparison'`).

- [ ] **Step 3: Implement** `comparison` and the one line in `wrapped.stats`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/census.py app/wrapped.py tests/test_census.py tests/test_wrapped.py
git commit -m "Compare a member's services with the census on the Wrapped card"
```

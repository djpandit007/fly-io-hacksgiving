# Design

## Context

Fine Print currently runs as a single FastAPI application on one Fly.io Sprite (`fly-io-hacksgiving`), backed by local SQLite (`fineprint.db`, schema `v5`) and Fly.io Connectors for OpenCode (TypeSafe Jev + DeepSeek/GLM models), Pinecone, AgentMail, Exa, and LlamaCloud. Code owns the workflow (`app/agent.py`, `app/ask.py`, `app/intake.py`, `app/bot.py`), external responses are validated with Pydantic at the boundary, and `app/sprite.py` holds Sprites task locks over `/.sprite/api.sock` so webhooks do not sleep mid-job.

See `proposal.md` for motivation and capability scope. This design explains how to evolve Fine Print into a 3-role multi-Sprite swarm, expand the legal engine across policy bundles, domain playbooks, statutory checks, benchmarking, and closed-loop counter-measures, and expose a live web Control Room on the Gateway Sprite's HTTPS URL—while preserving Fine Print's deterministic workflow discipline and zero-retention privacy model for personal documents.

## Goals / Non-Goals

**Goals:**
- Enforce security boundaries at the VM level using separate Fly.io Sprites, scoped Connectors, per-job DNS egress policies, and ~300ms base-checkpoint restores.
- Keep `code owns the workflow`: Jev makes narrow typed decisions (`noul`, `choice`, `score`); deterministic Python orchestrates Sprites, verifies verbatim quotes, enforces approval tokens, and computes benchmark percentiles.
- Maintain strict backward compatibility: existing single-Sprite deployments, local `uv run pytest`, and current Telegram/email flows continue to work cleanly with graceful degradation when worker Sprites are unconfigured.
- Keep personal uploaded contracts strictly ephemeral (processed in memory / quarantined worker scratch space and wiped via checkpoint restore; never stored in SQLite or Pinecone).

**Non-Goals:**
- **No Per-Member "Personal Legal Vault" Sprites**, **no Box Connector**, and **no local Ollama vault integration**.
- **No persistent storage of uploaded user contracts, screenshots of user documents, or outgoing notice bodies.**
- **No Node/SPA frontend build pipeline**: the web Control Room uses server-rendered Jinja2 HTML + lightweight HTMX/SSE served directly from FastAPI so deployment stays a single `uv sync`.
- **No autonomous legal dispatch without explicit human confirmation**: every outbound opt-out email or web-form submission requires a member-bound, single-use signed approval tap.

## Decisions

### D1. 3-Role Multi-Sprite Swarm Topology & Least-Privilege Connector Matrix

We introduce `app/swarm.py` to manage programmatic calls to the Sprites API (`https://api.sprites.dev/v1/sprites/...`) via a dedicated Sprites API Custom Connector (or `FP_SPRITES_TOKEN` in local dev) scoped to the `fly-io-hacksgiving-*` prefix.

```
+---------------------------------------------------------------------------------+
|                         SPRITE ROLE & CONNECTOR MATRIX                          |
+----------------------+-------------------------------+--------------------------+
| Sprite Role          | Attached Fly.io Connectors    | Network / Lifecycle      |
+----------------------+-------------------------------+--------------------------+
| 1. Gateway / Commons | OpenCode, Pinecone, Exa,      | Public HTTPS (:8080)     |
| fly-io-hacksgiving   | LlamaCloud, AgentMail (read), | Persistent SQLite disk   |
|                      | Sprites API (prefix-scoped)   | Sleeps when idle         |
+----------------------+-------------------------------+--------------------------+
| 2. Quarantine Scout  | NONE (zero connectors,        | Private only             |
| fly-io-hacksgiving-  | zero credentials on box)      | Per-job DNS egress lock  |
| scout                |                               | Base checkpoint restore  |
|                      |                               | after EVERY job (~300ms) |
+----------------------+-------------------------------+--------------------------+
| 3. Advocate Executor | AgentMail (send-only scope),  | Private only             |
| fly-io-hacksgiving-  | OpenCode (redline/simulation) | Triggered ONLY with      |
| advocate             |                               | signed approval token;   |
|                      |                               | checkpoint restore after |
|                      |                               | sensitive contract jobs  |
+----------------------+-------------------------------+--------------------------+
```

* **How Gateway talks to Workers:** Each worker Sprite runs a tiny internal runner script (`workers/scout_runner.py`, `workers/advocate_runner.py`) invoked via `POST /v1/sprites/{name}/exec` (passing structured JSON via stdin/args and reading typed Pydantic JSON from stdout) OR an internal authenticated HTTP endpoint on the worker Sprite. Using `exec` keeps worker URLs completely non-public (zero external attack surface).
* **Per-Job DNS Egress Locking (Scout):** Before executing a crawl for `target_url`, the Gateway validates `target_url` with `app/netguard.py` (public IP check), extracts the registrable target host (plus optional CDN/asset hosts if needed for JS bundles, capped tightly), updates the Scout Sprite's network policy via the Sprites API to allow only that host, runs `scout_runner.py`, and in a `finally:` block calls `POST /v1/sprites/{scout}/checkpoints/{base_id}/restore`.
* **Evidentiary Snapshots:** When Scout crawls a public terms page with Playwright, it saves a PNG screenshot and computes `sha256(normalised_text)`. For public service pages, screenshots are stored under `$FP_DATA_DIR/evidence/<service_id>/<doc_role>/<sha>.png` on the Gateway Sprite and served read-only in the Control Room. For private user links/PDFs, no screenshot or text is persisted.
* **Why `exec` + pre-warmed named workers over creating brand-new Sprites per request:** Creating a fresh Sprite from scratch installs Playwright/Chromium/LibreOffice (minutes) and hits per-minute creation rate limits. Pre-provisioning named Scout and Advocate Sprites once via `scripts/setup_swarm.sh`, taking a verified `base` checkpoint, and restoring that checkpoint in ~300ms after every job gives identical fresh-VM isolation with sub-second turnaround and $0 idle cost.
* **Alternative considered:** Running Playwright and LibreOffice inside the Gateway Sprite. Rejected because untrusted URLs/PDFs would share the same filesystem, process memory, and Connector reachability as SQLite and member records.

### D2. SQLite Schema Migration `v6` & Policy Bundles

`app/db.py` bumps `SCHEMA_VERSION` from `5` to `6` with an automatic transactional migration (preserving the existing pre-migration `.v5.bak` backup behavior):

```sql
-- Multi-document Policy Bundle per service
CREATE TABLE service_documents (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id),
    role        TEXT NOT NULL,          -- terms | privacy | ai_terms | billing | dpa
    url         TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    UNIQUE (service_id, role),
    UNIQUE (service_id, url)
);
-- Backfill every existing service's terms_url into service_documents(role='terms')
INSERT INTO service_documents (service_id, role, url, added_at)
    SELECT id, 'terms', terms_url, added_at FROM services;

-- Link snapshots and changes to their specific bundle document (NULL defaults to primary terms)
ALTER TABLE snapshots ADD COLUMN document_id INTEGER REFERENCES service_documents(id);
ALTER TABLE snapshots ADD COLUMN evidence_path TEXT; -- relative path for public evidentiary PNG
ALTER TABLE changes   ADD COLUMN document_id INTEGER REFERENCES service_documents(id);

-- Cross-document contradictions detected within one service's Policy Bundle
CREATE TABLE document_contradictions (
    id            INTEGER PRIMARY KEY,
    service_id    INTEGER NOT NULL REFERENCES services(id),
    doc_a_id      INTEGER NOT NULL REFERENCES service_documents(id),
    doc_b_id      INTEGER NOT NULL REFERENCES service_documents(id),
    heading_a     TEXT NOT NULL,
    heading_b     TEXT NOT NULL,
    quote_a       TEXT NOT NULL,
    quote_b       TEXT NOT NULL,
    summary       TEXT NOT NULL,
    severity      REAL NOT NULL,
    detected_at   TEXT NOT NULL,
    UNIQUE (service_id, doc_a_id, doc_b_id, heading_a, heading_b)
);

-- Per-member jurisdiction (code only, e.g. US-CA, US-NY, US-OTHER, EU, UK)
ALTER TABLE users ADD COLUMN jurisdiction TEXT;

-- Single-use signed human-approval tokens for Advocate actions (opt-outs, redline dispatch)
CREATE TABLE approval_tokens (
    token_hash    TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    action_kind   TEXT NOT NULL,        -- opt_out_email | opt_out_form | redline_kit
    payload_json  TEXT NOT NULL,        -- structured public/ephemeral parameters, expired & pruned
    expires_at    TEXT NOT NULL,
    consumed_at   TEXT
);

-- Metadata-only proof-of-notice receipts (never stores notice body or private contract text)
CREATE TABLE action_receipts (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    service_name  TEXT NOT NULL,
    action_kind   TEXT NOT NULL,        -- opt_out_email | opt_out_form | gdpr_delete | ai_opt_out
    target        TEXT NOT NULL,        -- recipient address or form host
    clause_sha    TEXT NOT NULL,
    proof_ref     TEXT NOT NULL DEFAULT '', -- AgentMail message_id or screenshot sha256
    executed_at   TEXT NOT NULL
);

-- Short-lived magic links and active Control Room sessions
CREATE TABLE dashboard_sessions (
    token_hash    TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    kind          TEXT NOT NULL,        -- magic | session
    expires_at    TEXT NOT NULL,
    consumed_at   TEXT
);

-- Sanitized swarm telemetry (zero URLs, zero member ids, pruned after 90 days)
CREATE TABLE swarm_events (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    sprite_role   TEXT NOT NULL,        -- gateway | scout | advocate
    event         TEXT NOT NULL,        -- wake | egress_lock | run | checkpoint_restore | sleep | error
    detail        TEXT NOT NULL DEFAULT '', -- generic label only, e.g. "playwright_crawl", never URLs/PII
    duration_ms   INTEGER
);
```

* **Policy Bundle Discovery (`app/discover.py`):** When watching a service, `discover_bundle(name_or_url, cl, swarm)` finds the primary `terms` URL (existing flow), then inspects links on the terms page and legal hub (via direct fetch or Scout Playwright crawl) for companion documents matching `privacy`, `ai_terms`, `billing`, and `dpa` on the same registrable domain (all vetted by `app/netguard.py`).
* **Cross-Document Contradictions (`app/agent.py` + `app/judge.py`):** When any document in a service's Policy Bundle changes materially, code retrieves top candidate sections from the *other* active documents in the same service (keyword/category match + Pinecone within that service), and Jev answers a targeted `noul` (`contradicts`) + `score` (`severity`) question on `(section_a, section_b)`. Material contradictions (`contradicts >= 0.75`) are stored once in `document_contradictions` and fanned out to watchers.
* **`/ask` Integration (`app/ask_tools.py`):** `list_sections`, `read_section`, `search_terms`, and `recent_changes` are updated to span all `service_documents` for watched services and include `doc_role` in every `Clause` citation label (e.g., `Notion (AI Terms) · Data Usage · as of 27 Nov 2026`).

### D3. Domain-Aware Legal Playbooks, Statutory Checks & Ephemeral Conflict Detection

We add `app/playbooks.py` as the single source of truth for all five legal domains:

1. **Document Router (`judge.classify_domain`)**: One Jev `choice` call over the document header/sample (`<=12k` chars) selecting among:
   - `consumer_tos` (existing 6 flags preserved verbatim for continuity)
   - `privacy_and_ai` (`ai_model_training`, `data_broker_sale`, `biometric_or_location`, `indefinite_retention`, `cross_context_ads`, `no_deletion_right`)
   - `residential_lease` (`auto_lease_renewal`, `repair_cost_shifting`, `early_termination_penalty`, `non_refundable_fees`, `landlord_unannounced_entry`, `habitability_waiver`)
   - `employment_or_nda` (`side_project_ip_capture`, `non_compete_clause`, `broad_non_solicit`, `equity_or_bonus_clawback`, `perpetual_confidentiality`, `mandatory_arbitration`)
   - `freelance_or_saas` (`uncapped_indemnity`, `asymmetric_liability_cap`, `extended_payment_terms`, `pre_payment_ip_transfer`, `no_kill_fee_termination`, `auto_renewal_lockin`)
2. **Fixed Plain-English Explanations (`FLAG_EXPLANATIONS`)**: Every flag key across all 5 domains has a static entry in `app/playbooks.py` verified by unit test to be `<= 200` characters, exported back into `app/judge.py` so existing `taps.build("fx", key)` buttons work unchanged.
3. **Jurisdiction Statutory Ruleset (`app/playbooks.py`)**: A curated, deterministic table mapping `(jurisdiction, domain, flag_key)` to concrete statutory references and a narrow Jev confirmation question (e.g., `("US-CA", "employment_or_nda", "non_compete_clause")` -> CA Bus. & Prof. Code § 16600; `("US-CA", "employment_or_nda", "side_project_ip_capture")` -> CA Labor Code § 2870; `("US-CA", "consumer_tos", "auto_renews")` -> California ARL; `("EU", "consumer_tos", "arbitration")` -> EU Unfair Contract Terms Directive 93/13/EEC; `("EU", "privacy_and_ai", "ai_model_training")` -> GDPR Art. 6 / Art. 21 right to object). When a member has a jurisdiction set and a mapped flag fires (`>= 0.7`), Jev runs a single `noul` check confirming whether the quoted clause triggers that statutory rule, and the verdict attaches a clear plain-English statutory note + disclaimer.
4. **Ephemeral Cross-Contract Conflict Check (`app/playbooks.py` + `app/agent.py`)**: When a member uploads an `employment_or_nda` or `freelance_or_saas` document, code inspects the member's *currently watched* services' stored `ai_terms`/`privacy`/`terms` sections touching data/IP/content licences, runs a bounded Jev `noul` conflict batch (max 8 candidate service clauses), and appends any confirmed conflicts (`>= 0.75`) to the verdict card. The uploaded contract text lives only in local variables for the request and is never written to disk, SQLite, or Pinecone.

### D4. Closed-Loop Counter-Measures & Adversarial Dual-Sprite Redlining (`app/advocate.py`)

* **Opt-Out / Rights Notice Flow:**
  1. When a material change has `opt_out >= 0.7` (or a checked policy triggers `ai_model_training` / `no_deletion_right` with an opt-out path), a structured Pydantic extractor (`extract_opt_out`) parses `{channel: "email" | "web_form" | "postal", target, deadline_days, required_fields, verbatim_instruction}`, verifying `verbatim_instruction` against the source text using `app/quotes.py`.
  2. Telegram shows `[Review & Draft Opt-Out]`. Tapping it drafts the notice (using member's name & verified email if linked, or asking for missing fields), stores a short-lived (15 min) single-use `approval_tokens` row (keyed by SHA-256 of a random 128-bit secret bound to `user_id`), and replies with the full preview + `[Approve & Send]` / `[Cancel]`.
  3. On `[Approve & Send]`, Gateway atomically marks `consumed_at` in SQLite inside a transaction (`WHERE token_hash = ? AND user_id = ? AND consumed_at IS NULL AND expires_at > ?`), then invokes the **Advocate Sprite** with the approved payload.
  4. For `email`: Advocate sends via its send-scoped AgentMail Connector (CC'ing the member's verified email) and returns `message_id`. For `web_form`: Advocate locks DNS egress to the form's verified host, runs Playwright to submit the form, captures a confirmation screenshot PNG, returns the PNG + SHA-256 directly to Gateway to send to the member in Telegram, and immediately restores its clean base checkpoint.
  5. Gateway inserts a metadata-only row in `action_receipts` and deletes the expired payload from `approval_tokens`.
* **Adversarial Dual-Sprite Redline & Negotiation Kit Flow:**
  1. On personal contract verdict cards (`residential_lease`, `employment_or_nda`, `freelance_or_saas`) with `>= 1` found flag, Telegram offers `[Generate Redline Kit]` -> tone buttons (`Polite`, `Balanced`, `Firm`).
  2. Because personal contracts are not stored on disk, the member triggers generation either directly from an inline ephemeral cache (in-RAM TTL 10 min, keyed by member-bound token, wiped on completion or expiry) or by re-sending the file with `/redline`.
  3. Gateway invokes the Advocate Sprite for the **Adversarial Dual-Sprite Simulation**:
     - **Pass 1 (Counterparty Counsel — isolated run A):** Given the flagged clauses, identifies the worst-case legal/financial exploit the counterparty could invoke under the current wording.
     - **Pass 2 (Consumer Counsel — isolated run B):** Drafts the narrowest, commercially realistic replacement wording to close each exploit at the chosen leverage tone (`Polite` / `Balanced` / `Firm`).
     - **Jev Risk Delta (`judge.redline_delta`):** Scores original vs. proposed clause severity (`score`) and verifies every original clause quote matches the source document.
  4. On the Advocate Sprite, `workers/advocate_runner.py` uses `python-docx` to generate a real OOXML tracked-changes `.docx` (`w:del` / `w:ins` + margin comments explaining each change) and LibreOffice headless (`soffice --headless --convert-to pdf`) to render an annotated `.pdf`, plus a ready-to-send negotiation email.
  5. Gateway delivers the `.docx`, `.pdf`, and email text to the member in Telegram (using Telegram `sendDocument`), and the Advocate Sprite immediately restores its clean base checkpoint so zero contract bytes remain on disk.

### D5. Pinecone Clause Market Benchmarking (`app/benchmark.py`)

* Extend `app/memory.py` `ClauseMetadata` with `domain: str = "consumer_tos"`, `severity: float = 0.0`, `favours: str = "neutral"`, `has_opt_out: bool = False`, and `public: bool = True`.
* Add `app/benchmark.py`:
  - `benchmark_clause(gw, text, category, severity, exclude_service=None)` queries Pinecone (`topK=50`, filter `{"public": True, "category": category}`) for comparable public clauses.
  - Requires at least `MIN_COHORT = 10` distinct services in the returned neighborhood; otherwise returns `None` (no fabricated stats).
  - Computes deterministic percentile rank of `severity` (and opt-out rarity) over the cohort's metadata and returns a typed `Benchmark(percentile, cohort_size, opt_out_share, comparable_services)`.
  - Private uploaded documents only *query* Pinecone (`log_body=False`) and are never upserted.

### D6. Live Control Room & Member Terms Radar (`app/web.py`)

* Served directly on the Gateway Sprite's existing FastAPI app (`:8080`), using Jinja2 templates (`app/templates/`) + HTMX + vanilla EventSource (SSE) — no build step.
* **No public data routes (decided during implementation):** every page needs a signed-in member and shows only that member's own data. `GET /` redirects to `/room`.
* **Member-scoped routes:**
  - **Terms Radar** (inside `/room`): the member's watched services only (sorted by worst recent severity), Policy Bundle coverage badges (`ToS`, `Privacy`, `AI`, `Billing`), ToS;DR grades, recent material changes, cross-document contradictions, and a live sanitized **Swarm Activity Strip**.
  - `GET /changes/{change_id}` — **Interactive Change & Evidence Inspector**, only for changes on a service the member watches (404 otherwise): Side-by-side redline diff, Jev calibrated probability bars (`material`, `severity`, `favours`, `opt_out`), Pinecone benchmark badge, evidentiary screenshot preview (when captured by Scout), and contradiction links.
  - `GET /api/swarm/stream` — Server-Sent Events stream (signed-in members only) pushing sanitized `swarm_events` in real time as Sprites wake, lock egress, run, checkpoint-restore, and sleep.
* **Authenticated Member Routes (magic-link + HTTP-only cookie):**
  - `/dashboard` in Telegram creates a `dashboard_sessions(kind='magic')` token (10-min TTL, single-use) and sends `https://<FP_PUBLIC_URL>/auth/magic?token=<raw>`.
  - `GET /auth/magic` atomically consumes the magic token, issues a 24-hour signed HTTP-only `SameSite=Lax` `Secure` session cookie backed by `dashboard_sessions(kind='session')`, and redirects to `/room`.
  - `GET /room` — **Member Control Room**: Shows ONLY that member's watched service bundles, upcoming deadlines + `.ics` link, configured jurisdiction, and metadata-only `action_receipts` log.
  - `users.remove()` deletes all `dashboard_sessions` rows for the removed user so revocation is immediate.

## Risks / Trade-offs

- **[Sprites API / Worker latency or flakiness]** → Pre-warm named Scout & Advocate Sprites from base checkpoints (~300ms restore instead of cold provisioning); keep explicit timeouts (`SCOUT_TIMEOUT=45s`, `ADVOCATE_TIMEOUT=120s`); fall back cleanly to Exa/direct fetch for crawling and clear user messaging for Advocate actions; never crash daily ticks.
- **[Prompt injection inside untrusted legal pages or user PDFs]** → Hardware isolation enforces defense in depth: Scout has zero Connectors and a per-job DNS egress lock; Advocate only runs on structured, verified payloads gated by DB-enforced single-use signed approval tokens; all LLM inputs wrap untrusted text in data-only XML tags (matching `app/ask.py`) and verify quotes against source text.
- **[Ephemeral contract text needed between Verdict Card and `[Generate Redline Kit]` tap]** → Hold an encrypted-in-RAM (process memory only, never disk/SQLite) LRU cache keyed by `(user_id, ephemeral_token)` with a 10-minute TTL and 20 MB cap, wiped immediately after redline generation or on Sprite sleep. If the Sprite slept before the member tapped, politely ask them to re-send the file with `/redline`.
- **[Playwright opt-out web forms vary wildly across sites]** → Restrict Playwright form automation to well-structured forms whose submit target matches the service's verified domain, require a dry-run preview of extracted fields before approval, and degrade cleanly to email or printable notice whenever a CAPTCHA or login wall blocks automation.
- **[Statutory unenforceability notes sounding like legal advice]** → Restrict statutory annotations to a curated, high-confidence ruleset (`app/playbooks.py`) verified by Jev against the quoted clause, phrase every note carefully ("may be restricted or void under…"), and always display `Information, not legal advice.`
- **[Control Room leaking another member's data]** → No public data routes; every query is scoped to the signed-in member's watched service ids, changes on unwatched services return 404, verified by cross-member isolation tests.

## Migration Plan

1. **Database Upgrade:** On startup, `db.connect()` backs up `fineprint.db` to `fineprint.db.v5.bak` and runs `V6_MIGRATION` transactionally, populating `service_documents` from existing `services.terms_url` and defaulting `users.jurisdiction` to `NULL`.
2. **Swarm Provisioning (`scripts/setup_swarm.sh`):**
   - Creates/updates `fly-io-hacksgiving-scout` (installs Python 3.12 + Playwright + Chromium, zero Connectors) and creates its clean `base` checkpoint, saving the checkpoint ID into `.env.sprite` (`FP_SCOUT_BASE_CHECKPOINT`).
   - Creates/updates `fly-io-hacksgiving-advocate` (installs Python 3.12 + `python-docx` + LibreOffice headless + Playwright, attaches send-scoped AgentMail + OpenCode Connectors) and creates its `base` checkpoint (`FP_ADVOCATE_BASE_CHECKPOINT`).
   - Updates `fly-io-hacksgiving` (Gateway) with the prefix-scoped Sprites API connector/token and redeploys.
3. **Rollback:** Because `V6_MIGRATION` only adds tables and nullable columns (never dropping `services.terms_url` or existing columns), reverting the Gateway image + restoring `fineprint.db.v5.bak` cleanly restores single-Sprite v5 operation.
4. **Phased Delivery Order for Implementing Agents:**
   - **Phase 1:** DB `v6` migration + Policy Bundles (`service_documents`, discovery, shared watching, contradiction detection, `/ask` bundle support).
   - **Phase 2:** Legal Playbooks (`app/playbooks.py`), Document Router, Jurisdiction (`/jurisdiction` + statutory checks), Ephemeral Cross-Contract Conflicts, and Pinecone Benchmarking (`app/benchmark.py`).
   - **Phase 3:** Multi-Sprite Swarm orchestration (`app/swarm.py`, `workers/scout_runner.py`, `workers/advocate_runner.py`, `scripts/setup_swarm.sh`, DNS egress lock + checkpoint restore + metrics).
   - **Phase 4:** Closed-Loop Counter-Measures (`app/advocate.py`: opt-out extraction, signed approval tokens, AgentMail/Playwright dispatch, receipts, adversarial dual-Sprite `.docx`/`.pdf` redline kits).
   - **Phase 5:** Live Control Room & Member Terms Radar (`app/web.py`, Jinja2/HTMX templates, `/dashboard` magic links, `/api/swarm/stream` SSE, end-to-end tests & README update).

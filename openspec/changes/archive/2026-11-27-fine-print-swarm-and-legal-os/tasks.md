# Tasks

## 1. Database v6 Migration & Policy Bundles

- [x] 1.1 Add `V6_MIGRATION` and `SCHEMA_VERSION = 6` in `app/db.py` (`service_documents`, `document_contradictions`, `users.jurisdiction`, `approval_tokens`, `action_receipts`, `swarm_events`, plus nullable `document_id` / `evidence_path` columns on `snapshots` and `changes`) with automatic backfill of existing `services.terms_url` into `service_documents(role='terms')`, and verify `uv run pytest` passes a new migration test upgrading a v5 fixture database with `.v5.bak` preserved.
- [x] 1.2 Extend `app/discover.py` with `discover_bundle()` to resolve companion legal documents (`privacy`, `ai_terms`, `billing`, `dpa`) on the service's own registrable domain while enforcing `app/netguard.py` public-address validation, and verify unit tests confirm safe companion discovery and rejection of private/loopback links.
- [x] 1.3 Update `app/agent.py` (`onboard_service`, `check_service`, `record_version`, `ChangeReport`) to fetch, diff, and judge every document in a service's Policy Bundle once per service, labelling alerts with document role, and verify unit tests confirm shared-once judging and backward compatibility for single-document services.
- [x] 1.4 Implement cross-document contradiction detection in `app/judge.py` and `app/agent.py` so material updates are checked against other active documents in the same Policy Bundle, storing confirmed contradictions (`>= 0.75`) in `document_contradictions` and fanning out alerts citing both documents; verify unit tests cover both contradiction detection and quiet non-contradiction cases.
- [x] 1.5 Update `app/ask_tools.py` and `app/ask.py` so `/ask` searches, reads, verifies quotes, and attributes citations across all `service_documents` roles (`terms`, `privacy`, `ai_terms`, `billing`, `dpa`), updating the not-found message accordingly, and verify `uv run pytest` passes updated `/ask` tests.

## 2. Domain-Aware Legal Playbooks, Statutory Checks & Benchmarking

- [x] 2.1 Create `app/playbooks.py` defining the five legal domains (`consumer_tos`, `privacy_and_ai`, `residential_lease`, `employment_or_nda`, `freelance_or_saas`), their Jev red-flag batteries, and fixed plain-English `FLAG_EXPLANATIONS` exported via `app/judge.py`; verify a unit test enforces that every flag across all five domains has an explanation `<= 200` characters.
- [x] 2.2 Add `judge.classify_domain()` (Jev `choice` router) and wire `agent.verdict_card()` to classify incoming links/documents, run the domain-specific flag battery, verify verbatim quotes, and attach domain-appropriate `💡` explain buttons; verify tests confirm lease, employment/NDA, and freelance/SaaS documents trigger domain-specific flags while falling back to `consumer_tos` when uncertain.
- [x] 2.3 Add `/jurisdiction` command handling in `app/bot.py` and `app/users.py` (`US-CA`, `US-NY`, `US-OTHER`, `EU`, `UK`, or clear) plus the deterministic statutory ruleset and Jev confirmation check in `app/playbooks.py` and `app/agent.py`, verifying unit tests confirm California/EU statutory notes appear with the disclaimer and invalid codes are rejected.
- [x] 2.4 Implement ephemeral cross-contract conflict checking in `app/playbooks.py` and `app/agent.py` so uploaded `employment_or_nda` and `freelance_or_saas` contracts are compared in RAM against the member's watched services' active policies without persisting uploaded text; verify unit tests confirm conflict warnings stay private to the uploader and zero contract text is written to SQLite or Pinecone.
- [x] 2.5 Extend `app/memory.py` `ClauseMetadata` (`domain`, `severity`, `favours`, `has_opt_out`, `public`) and create `app/benchmark.py` to compute deterministic Pinecone market percentile ranks (requiring `MIN_COHORT = 10` public services and never upserting private uploads); verify unit tests cover percentile calculation, small-cohort omission, and private-upload read-only queries.

## 3. Multi-Sprite Swarm Infrastructure & Telemetry

- [x] 3.1 Add Sprites API configuration fields in `app/config.py` (`FP_SPRITES_API_BASE`, `FP_SPRITES_TOKEN`, `FP_SCOUT_SPRITE`, `FP_SCOUT_BASE_CHECKPOINT`, `FP_ADVOCATE_SPRITE`, `FP_ADVOCATE_BASE_CHECKPOINT`) and create `app/swarm.py` with typed clients for `exec`, per-job DNS network policy locking, checkpoint restore, and graceful degradation when unconfigured; verify unit tests with mocked HTTP responses confirm DNS lock + `finally` checkpoint restore even on worker errors.
- [x] 3.2 Create `workers/scout_runner.py` (Playwright headless crawl, accordion expansion, companion policy link extraction, normalised text extraction, SHA-256 digest, and PNG evidentiary screenshot) and wire `agent.page_text()` to invoke the Ephemeral Scout Sprite before falling back to Exa/LlamaParse; verify tests confirm Scout fallback, evidentiary PNG persistence for public pages only, and zero retention for private checks.
- [x] 3.3 Record sanitized `swarm_events` and daily swarm/counter-measure counts (Scout jobs, DNS egress locks, checkpoint restore avg/max ms, Advocate redline kits, approved opt-out dispatches) in `app/metrics.py`, update `/stats` output, and verify unit tests assert zero URLs, file names, or member ids appear in `swarm_events` or `metric_counts`.
- [x] 3.4 Create `scripts/setup_swarm.sh` and update `README.md` with setup instructions for provisioning `fly-io-hacksgiving-scout` (zero Connectors, Playwright/Chromium, base checkpoint), `fly-io-hacksgiving-advocate` (send-scoped AgentMail + OpenCode Connectors, `python-docx` + LibreOffice + Playwright, base checkpoint), and the prefix-scoped Sprites API Connector on the Gateway; verify `bash -n scripts/setup_swarm.sh` succeeds.

## 4. Closed-Loop Legal Counter-Measures & Advocate Redline Kits

- [x] 4.1 Implement opt-out and statutory-rights extraction (`extract_opt_out` with verbatim quote verification) and single-use, member-bound, expiring `approval_tokens` management in `app/advocate.py`, adding `[Review & Draft Opt-Out]` and `[Approve & Send]` callback handlers in `app/bot.py` and `app/taps.py`; verify unit tests confirm replay/cross-member token rejection and postal-only fallback messaging.
- [x] 4.2 Create `workers/advocate_runner.py` supporting (a) AgentMail send-scoped opt-out dispatch (CC'ing member's verified email) and (b) domain-locked Playwright web-form opt-out execution returning a confirmation screenshot PNG, followed by base-checkpoint restore and metadata-only `action_receipts` logging; verify unit tests cover both dispatch paths and confirm notice bodies are never stored in SQLite.
- [x] 4.3 Implement the Adversarial Dual-Sprite redline pipeline in `app/advocate.py` and `workers/advocate_runner.py` (Counterparty Counsel exploit pass, Consumer Counsel counter-clause pass, Jev risk-delta scoring, OOXML tracked-changes `.docx` via `python-docx`, LibreOffice headless `.pdf` export, and `Polite`/`Balanced`/`Firm` negotiation email) backed by a 10-minute RAM-only ephemeral cache and Telegram `sendDocument` delivery in `app/telegram.py` and `app/bot.py`; verify unit tests validate generated `.docx` tracked-change XML (`w:ins`/`w:del`), cache eviction, and immediate cleanup.

## 5. Bot Guidance & Scope Reductions

- [x] 5.4 Update `app/bot.py` (`MEMBER_MENU`, `HELP`) to publish `/jurisdiction` and `/redline`, updating README command docs and verifying bot-guidance tests pass.
- [x] 5.5 Remove the web Control Room built during implementation (`app/web.py`, `app/sessions.py`, templates, `/dashboard`, `dashboard_sessions`, `jinja2`), keeping receipts in Telegram and swarm telemetry in `/stats`; verify the full suite passes without it.
- [x] 5.6 Restrict worker egress to exact host names (no wildcard sub-domains) in `app/swarm.py`, `workers/scout_runner.py` and `workers/advocate_runner.py`, sending all Chromium traffic through a local egress proxy (`workers/egress_proxy.py`) that tunnels only to exact allowed hosts at their checked public addresses, and crawling a URL's HTTP landing page (`swarm.landing_url`); verify unit tests reject wildcard allowlists and cover the proxy's refusals.

## 6. Cross-Cutting Integration Verification

- [x] 6.1 Run the full test suite (`uv run pytest`) and linter (`uv run ruff check .`) and verify every existing and new spec scenario passes with zero warnings or regressions.
- [x] 6.2 Run an end-to-end local smoke test covering Policy Bundle watch + contradiction alert -> domain playbook verdict + statutory note + ephemeral conflict check -> signed opt-out approval + receipt -> redline kit `.docx` generation -> member-scoped receipts + sanitized swarm telemetry, verifying zero private document retention in SQLite after completion.

## Workflow follow-up

- Run `/opsx-apply` to implement the tasks in `openspec/changes/fine-print-swarm-and-legal-os/tasks.md`.
- After implementation and review pass, run `/opsx-archive` to sync the delta specs into `openspec/specs/` and archive the change.

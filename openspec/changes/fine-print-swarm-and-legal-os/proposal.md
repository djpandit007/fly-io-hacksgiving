# Proposal

## Why

Fine Print today is a strong single-Sprite Telegram and email bot that watches one terms page per service and flags six generic consumer issues, then stops before helping the user act. To stand out as a category-defining agent for Fly.io Hacksgiving (and genuinely protect users in practice), Fine Print needs to evolve from a passive notifier on one VM into a multi-Sprite Personal Legal OS that uses Sprites' hardware isolation, per-Sprite DNS egress policies, scoped Connectors, and ~300ms checkpoints as its core security and execution model — while expanding across the full consumer and personal legal surface area with closed-loop counter-measures and an interactive web Control Room.

## What Changes

- **Multi-Sprite Functional Swarm (Gateway + Scout + Advocate)**:
  - **Gateway / Commons Sprite**: Retains Telegram/AgentMail webhooks, shared SQLite corpus, member routing, and serves the new live, per-member web Control Room (with each member's own Terms Radar) on `:8080`.
  - **Ephemeral Quarantine & Dark-Pattern Scout Sprites**: Hardware-isolated worker Sprites with **zero Connectors** and a **per-job DNS egress policy** locked to the target domain. Runs headless Chromium (Playwright) to crawl JS-heavy legal portals, expand hidden accordions, discover linked policies, capture timestamped evidentiary screenshots, and safely parse untrusted user links/PDFs — restoring to a clean base checkpoint in ~300ms after every job.
  - **Advocate / Execution Sprite**: Role-isolated Sprite holding **send-scoped action Connectors** (AgentMail send, Playwright form automation) and a real Linux document toolchain (`python-docx`, LibreOffice headless) to produce tracked-changes redlines and execute user-approved opt-outs. Also supports **Adversarial Dual-Sprite Simulation** (forking two sibling Sprites from a checkpoint — Company Counsel vs. Consumer Counsel — scored by Jev risk delta).
- **Multi-Document Service Policy Bundles & Cross-Document Contradictions**:
  - **BREAKING (data model extension)**: Services expand from a single `terms_url` to a **Policy Bundle** (`terms`, `privacy`, `ai_terms`, `billing`, `dpa`), fetched and judged once per service, detecting contradictions across documents (e.g., ToS claims user ownership while AI Addendum claims a perpetual training license).
- **Domain-Aware Legal Playbooks & Statutory Unenforceability Checks**:
  - Jev classifies incoming links and uploaded documents via a `choice` Document Router into `consumer_tos`, `privacy_and_ai`, `residential_lease`, `employment_or_nda`, or `freelance_or_saas`, running tailored Jev red-flag batteries and fixed plain-English explanations (`<=200` chars) for each domain.
  - Members configure their jurisdiction (`/jurisdiction`, e.g., `US-CA`, `US-NY`, `EU`, `UK`) so Jev flags clauses likely void or unenforceable locally (e.g., California non-compete / Labor Code § 2870 IP overreach, FTC/ARL auto-renewal, EU unfair arbitration).
  - **Ephemeral Cross-Contract Conflict Checks**: When a member uploads a personal contract (e.g., an employer NDA), Fine Print ephemerally cross-checks it against the member's watched SaaS services' terms without persisting the uploaded document.
- **Closed-Loop Legal Counter-Measures**:
  - **1-Tap Opt-Out & Rights Execution**: When Jev detects an opt-out or statutory right (`opt_out >= 0.7`, AI opt-out, GDPR/CCPA deletion), Fine Print extracts the exact notice target and required fields, drafts the formal notice, and—only after an explicit signed Telegram `[Approve & Send]` tap—dispatches it via AgentMail or Playwright form automation on the Advocate Sprite, returning a timestamped proof-of-notice dossier (copy to user + metadata receipt).
  - **Automated Contract Redlining & Negotiation Kit**: For personal contracts (leases, job offers, NDAs, freelance MSAs), the Advocate Sprite generates a real tracked-changes `.docx` and annotated `.pdf` plus a leverage-calibrated negotiation email (`Polite` / `Balanced` / `Firm`).
- **Pinecone Clause Market Benchmarking**:
  - Scores clauses against Fine Print's Pinecone corpus to tell members how aggressive or unusual a clause is relative to comparable services/contracts (percentile rank + peer summary).
- **Live Web Control Room & Member Terms Radar**:
  - A per-member **Terms Radar** inside the Control Room showing only that member's watched services, severity trends, ToS;DR grades, and recent worst material changes. Nothing is published to unauthenticated visitors.
  - Private member **Control Room** opened via a short-lived magic link from `/dashboard`, displaying side-by-side clause redlines with Jev probabilities, verified verbatim quotes, Pinecone benchmarks, opt-out action receipts, and live multi-Sprite swarm telemetry (waking, egress-locking, checkpointing, sleeping).
- **Explicit Non-Goals (Out of Scope)**:
  - **No Per-Member "Personal Legal Vault" Sprites**: Personal documents remain strictly ephemeral and are never persisted on disk; zero-retention privacy for uploaded files is preserved.
  - **No Box Connector** and **no local Ollama vault models**.
  - **No autonomous unapproved legal actions**: Every outbound email or web-form submission requires an explicit member confirmation tap carrying a signed, single-use approval token.
  - **Information and drafting assistance only, not legal advice.**

## Capabilities

### New Capabilities
- `sprite-swarm`: Multi-Sprite orchestration, per-Sprite connector isolation, per-job DNS egress locking, ephemeral Scout execution with base-checkpoint restore, Advocate execution sandbox, and adversarial dual-Sprite negotiation simulation.
- `legal-playbooks`: Jev document-domain classification, domain-specific red-flag batteries (ToS, Privacy/AI, Lease, Employment/NDA, Freelance/SaaS), jurisdiction-aware statutory unenforceability checks, and ephemeral cross-contract conflict detection against watched services.
- `legal-countermeasures`: Closed-loop opt-out/rights notice drafting and approval-gated dispatch (AgentMail and Playwright form automation) with proof-of-notice receipts, plus automated redlined `.docx`/`.pdf` and negotiation email generation.
- `clause-benchmarking`: Pinecone-backed market percentile benchmarking comparing clauses against comparable services and contract domains.
- `control-room-web`: Magic-link-authenticated, member-scoped Control Room (including the member's own Terms Radar) with interactive clause diffs, Jev scores, verified quotes, and live multi-Sprite swarm telemetry.

### Modified Capabilities
- `terms-watch`: Expand service watching from one `terms_url` to multi-document Policy Bundles (Terms, Privacy, AI Terms, Billing/DPA) with cross-document contradiction alerts while keeping shared-once judging and per-member isolation.
- `terms-discovery`: Resolve full Policy Bundles per service and add the Ephemeral Scout Sprite (Playwright + DNS egress lock + evidentiary screenshots) for JS-heavy and bot-walled legal portals.
- `flag-explanations`: Extend fixed plain-English `<=200`-char flag explanations and verified clause quoting to cover every domain-specific playbook flag.
- `bot-guidance`: Add `/jurisdiction` and `/dashboard` commands, command-menu entries, and help actions for the expanded legal engine and web Control Room.
- `owner-metrics`: Track multi-Sprite swarm runs, checkpoint restore durations, egress-lock counts, and counter-measure outcomes while keeping zero member content in metrics.

## Impact

- **Database (`app/db.py`)**: New schema migration (`v6`) adding `service_documents` (Policy Bundle docs per service), `document_contradictions`, `users.jurisdiction`, `approval_tokens` (short-lived HMAC/single-use action approvals), `action_receipts` (metadata-only proof of sent opt-outs), `dashboard_sessions` (magic-link tokens), and `swarm_events` (ephemeral telemetry for the Control Room and `/stats`).
- **Sprites Infrastructure (`app/sprite.py`, new `app/swarm.py`, `scripts/`)**: Programmatic Sprites API client (`https://api.sprites.dev/v1/sprites/...`) for waking/running commands on Scout & Advocate Sprites, applying DNS network policies, creating/restoring checkpoints, and provisioning scripts (`scripts/setup_swarm.sh`) for the 3 Sprite roles with scoped Fly.io Connectors.
- **Legal Engine (`app/judge.py`, `app/writer.py`, `app/agent.py`, `app/ask.py`, `app/ask_tools.py`, new `app/playbooks.py`, `app/advocate.py`, `app/benchmark.py`)**: Document router, domain playbooks, statutory ruleset, ephemeral conflict checker, opt-out extractor/dispatcher, adversarial dual-Sprite redliner, `.docx`/`.pdf` artifact builder, and Pinecone percentile benchmarking.
- **Web & Bot Surfaces (`app/main.py`, `app/bot.py`, `app/taps.py`, `app/notify.py`, `app/metrics.py`, new `app/web.py` + templates/static assets)**: FastAPI routes for `/` (redirect to the Control Room), `/room` (magic-link Control Room with the member's Terms Radar), `/api/swarm/stream` (SSE live Sprite telemetry), plus `/jurisdiction`, `/dashboard`, `[Approve & Send Opt-Out]`, and `[Generate Redline Kit]` Telegram flows.
- **External Dependencies**: Sprites API token (scoped to swarm prefix), Playwright + Chromium on Scout/Advocate Sprites, `python-docx` + LibreOffice headless on Advocate Sprite; continues using existing OpenCode (Jev + chat/verifier), Pinecone, AgentMail, Exa, LlamaCloud, and ToS;DR connectors.

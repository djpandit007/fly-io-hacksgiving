# Spec Delta

## Purpose

Coordinates Fine Print's multi-Sprite topology—Gateway, Ephemeral Quarantine Scout, and Advocate Execution Sprites—using hardware isolation, per-job DNS egress policies, scoped Connectors, and live checkpoint restore as its security and execution boundary.

## ADDED Requirements

### Requirement: Role-isolated Sprite topology with least-privilege Connectors
The system SHALL separate responsibilities across three Sprite roles: the Gateway Sprite, the Ephemeral Scout Sprite, and the Advocate Sprite. The Scout Sprite SHALL hold zero provider Connectors and zero member data. The Advocate Sprite SHALL hold only the send-scoped Connectors required for user-approved actions.

#### Scenario: Untrusted page crawl runs without credentials
- **WHEN** the Gateway dispatches a URL crawl or untrusted file parse to the Scout Sprite
- **THEN** the Scout executes with zero Fly.io Connectors attached and cannot send email, query Pinecone, or read member records

#### Scenario: Prompt injection inside a contract attempts outbound email
- **WHEN** an untrusted contract instructs the system to email its contents to an external address during analysis
- **THEN** no send Connector is reachable during analysis and no email is sent

### Requirement: Per-job DNS egress locking on the Scout Sprite
The system SHALL apply a per-job DNS network policy on the Scout Sprite before fetching an external URL so that only exact host names resolve: the target host, its registrable domain, and at most a few explicit extra hosts, never wildcard sub-domains. Every allowed host SHALL be resolved and checked once before the job, and the browser SHALL be pinned to those checked addresses. All other names SHALL fail closed at DNS resolution.

#### Scenario: Scout fetches a service's legal portal
- **WHEN** the Scout is assigned to crawl `https://www.notion.so/terms`
- **THEN** its DNS policy permits only `www.notion.so` and `notion.so`, and refuses requests to any other host, including other sub-domains such as `cdn.notion.so`

#### Scenario: Allowed host answers differently later
- **WHEN** an allowed host's DNS answer changes to a private address after the job starts
- **THEN** the browser keeps using the address checked before the job and never connects to the new one

#### Scenario: Scout attempts to reach a non-public address
- **WHEN** a target URL redirects to a private, link-local, or loopback address
- **THEN** the fetch is refused and aborted without returning content

### Requirement: Base-checkpoint restore after every Scout job
The system SHALL maintain a clean base checkpoint on the Scout Sprite and SHALL restore the Scout Sprite to that base checkpoint after every crawl or untrusted document parse so no files, browser state, or residual artifacts survive between jobs.

#### Scenario: Scout completes an untrusted PDF parse
- **WHEN** the Scout finishes parsing a member-submitted PDF and returns structured text to the Gateway
- **THEN** the Scout restores to its clean base checkpoint before accepting the next job and records the restore duration

### Requirement: Evidentiary page snapshots on the Scout Sprite
The system SHALL capture a rendered visual screenshot and a cryptographic SHA-256 hash of the extracted text whenever the Scout crawls a JS-rendered or bot-walled legal page, returning metadata so material changes and opt-out actions carry timestamped proof.

#### Scenario: JS-rendered terms page crawled by Scout
- **WHEN** a direct fetch yields insufficient text and the Scout renders the page in a headless browser
- **THEN** the Scout returns the normalised text, discovered policy links, a SHA-256 content digest, and a timestamped evidentiary screenshot

### Requirement: Adversarial dual-Sprite negotiation simulation
The system SHALL support running an adversarial redline simulation for personal contracts by forking two isolated Advocate worker runs from a clean checkpoint: one arguing as counterparty counsel to surface clause exploits and one arguing as consumer counsel to draft minimal counter-clauses, scored before and after by the judge model.

#### Scenario: Member requests an adversarial redline on a lease or offer
- **WHEN** a member requests a negotiation kit for an uploaded contract
- **THEN** the system runs counterparty and consumer counsel passes in isolation, scores the risk reduction, and includes the highest-impact counter-clauses in the redline output

### Requirement: Graceful fallback when worker Sprites are unavailable
The system SHALL degrade gracefully when worker Sprites are not configured or temporarily unreachable, falling back to existing single-Sprite fetching for public pages and clearly telling the member when Scout-only or Advocate-only actions cannot run.

#### Scenario: Local development without Sprites API token
- **WHEN** Fine Print runs locally without worker Sprites configured
- **THEN** existing watch, check, and `/ask` workflows continue to function and Advocate-only actions report that the action worker is unavailable

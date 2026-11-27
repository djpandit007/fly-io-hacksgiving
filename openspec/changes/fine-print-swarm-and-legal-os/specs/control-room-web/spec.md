# Spec Delta

## Purpose

Publishes an interactive web interface directly on the Gateway Sprite's HTTPS URL, serving a magic-link-authenticated, member-scoped Control Room (including that member's own Terms Radar) for clause diffs, Jev scores, verified quotes, action receipts, and live multi-Sprite telemetry.

## ADDED Requirements

### Requirement: Member-scoped Terms Radar
The system SHALL show each signed-in member a Terms Radar in their Control Room covering only the services on that member's own watchlist: ToS;DR grades, Policy Bundle coverage, recent material changes, cross-document contradictions, and Jev severity badges. The system SHALL NOT publish any page listing services, changes, or contradictions to unauthenticated visitors or to members who do not watch the service.

#### Scenario: Visitor opens the Sprite URL
- **WHEN** an unauthenticated visitor requests `/`
- **THEN** they are sent to the Control Room sign-in, which tells them to request a `/dashboard` link in Telegram, and no service or change data is shown

#### Scenario: Member opens their Terms Radar
- **WHEN** a signed-in member opens the Control Room
- **THEN** the Terms Radar lists only the services that member watches and none watched solely by other members

#### Scenario: Change diff inspector
- **WHEN** a signed-in member selects a material change on a service they watch
- **THEN** the page shows the side-by-side clause redline, Jev calibrated probabilities (`material`, `severity`, `favours`, `opt_out`), category, and benchmark percentile when available

#### Scenario: Change on a service the member does not watch
- **WHEN** a signed-in member requests a change on a service they do not watch, or someone requests a change without signing in
- **THEN** the system does not show it

### Requirement: Short-lived magic-link authentication for the member Control Room
The system SHALL issue a short-lived, single-use magic login link via `/dashboard` in Telegram that establishes an HTTP-only signed session cookie scoped strictly to that member's own Control Room (`/room`), rejecting missing, expired, or invalid tokens.

#### Scenario: Member opens Control Room via /dashboard
- **WHEN** a member sends `/dashboard` in Telegram and opens the returned link before expiry
- **THEN** the system exchanges the token for an authenticated session cookie and displays that member's watchlist, upcoming deadlines, action receipts, and jurisdiction setting

#### Scenario: Expired or replayed magic link
- **WHEN** someone opens a magic link after it expires or after it was already redeemed
- **THEN** the system denies access and instructs the member to request a fresh `/dashboard` link in Telegram

#### Scenario: Removed member session invalidated
- **WHEN** the owner removes a member with `/remove`
- **THEN** that member's active Control Room sessions are immediately rejected on subsequent requests

### Requirement: Live multi-Sprite swarm telemetry stream
The system SHALL publish a live read-only Server-Sent Events stream (`/api/swarm/stream`) and UI panel displaying sanitized multi-Sprite lifecycle events (Sprite role, state transitions such as waking, egress-locked, running, checkpoint-restoring, sleeping, and duration) with no private URLs, document content, or member identifiers.

#### Scenario: Scout Sprite wakes and restores during a check
- **WHEN** the Gateway dispatches a crawl to the Scout Sprite while a member's Control Room is open
- **THEN** connected browsers receive live sanitized telemetry events showing the Scout waking, locking DNS egress, completing, and restoring its base checkpoint

# Spec Delta

## Purpose

Publishes an interactive web interface directly on the Gateway Sprite's HTTPS URL, combining a public read-only Terms Radar of watched services with a magic-link-authenticated member Control Room for clause diffs, Jev scores, verified quotes, action receipts, and live multi-Sprite telemetry.

## ADDED Requirements

### Requirement: Public read-only Terms Radar
The system SHALL serve a public read-only Terms Radar page at the Gateway Sprite's root URL (`/`) showing indexed public services, ToS;DR grades, recent material changes, cross-document contradictions, and Jev severity badges without exposing any member identities, watchlists, private deadlines, or uploaded documents.

#### Scenario: Visitor opens the Sprite URL
- **WHEN** an unauthenticated visitor requests `/`
- **THEN** the page renders the public Terms Radar leaderboard and recent material change cards with zero member-specific information

#### Scenario: Public change diff inspector
- **WHEN** a visitor selects a public material change on the Terms Radar
- **THEN** the page shows the side-by-side clause redline, Jev calibrated probabilities (`material`, `severity`, `favours`, `opt_out`), category, and benchmark percentile when available

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
- **WHEN** the Gateway dispatches a crawl to the Scout Sprite while the Control Room or Terms Radar is open
- **THEN** connected browsers receive live sanitized telemetry events showing the Scout waking, locking DNS egress, completing, and restoring its base checkpoint

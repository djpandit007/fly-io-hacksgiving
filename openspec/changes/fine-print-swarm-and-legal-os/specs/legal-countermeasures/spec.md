# Spec Delta

## Purpose

Turns passive legal alerts and verdict cards into closed-loop member-approved counter-measures: formal opt-out and statutory rights notices with proof receipts, plus tracked-changes redlined contracts and negotiation emails generated on the Advocate Sprite.

## ADDED Requirements

### Requirement: Opt-out and rights notice extraction and drafting
The system SHALL, when a material terms change or checked document includes an actionable opt-out or statutory right (such as arbitration opt-out, AI-training opt-out, or GDPR/CCPA deletion), extract the target email address or web-form URL, deadline, and required identifying fields, and offer a draft notice to the member.

#### Scenario: Material arbitration change includes an opt-out email
- **WHEN** a watched service adds binding arbitration with a 30-day opt-out by email to `arbitration-optout@example.com`
- **THEN** the alert includes a `[Review & Draft Opt-Out]` button that previews the formatted legal notice citing the clause and target address

#### Scenario: Opt-out requires postal mail only
- **WHEN** a clause permits opting out only by physical mail
- **THEN** the system drafts the printable notice text and clearly states that electronic dispatch is not available

### Requirement: Cryptographically signed single-use human approval before dispatch
The system SHALL NOT send any outbound opt-out email or submit any external web form without an explicit confirmation tap from the owning member carrying a short-lived, single-use, member-bound signed approval token.

#### Scenario: Member confirms opt-out dispatch
- **WHEN** member A previews a drafted opt-out notice and taps `[Approve & Send]` within the token's expiry window
- **THEN** the Gateway validates the signature, marks the token consumed, and instructs the Advocate Sprite to dispatch the notice with a copy to member A's verified email

#### Scenario: Replayed or forged approval token
- **WHEN** a second tap reuses an already-consumed token or member B submits member A's token
- **THEN** the system rejects the request and dispatches nothing

### Requirement: Proof-of-notice receipt without retaining private content
The system SHALL record a minimal metadata receipt when an opt-out or rights notice is dispatched—storing only the member id, service name, action kind, target host or address, timestamp, clause SHA-256 digest, and delivery message id or screenshot hash—and deliver the full proof packet directly to the member.

#### Scenario: Opt-out email sent successfully
- **WHEN** the Advocate Sprite sends an approved opt-out via AgentMail
- **THEN** the member receives a confirmation message and email copy with the timestamp and clause hash, and only the metadata receipt is stored in SQLite

#### Scenario: Web-form opt-out executed via Playwright
- **WHEN** an approved opt-out targets an allowlisted web opt-out portal
- **THEN** the Advocate Sprite completes the form, captures a confirmation screenshot, returns the screenshot directly to the member in Telegram, and restores its clean state

### Requirement: Automated redlined contract and negotiation kit generation
The system SHALL allow a member checking a personal contract (`residential_lease`, `employment_or_nda`, or `freelance_or_saas`) to request a negotiation kit (`Polite`, `Balanced`, or `Firm`) that produces a tracked-changes `.docx`, an annotated `.pdf`, and a ready-to-send counter-proposal email via the Advocate Sprite, without persisting the uploaded contract after delivery.

#### Scenario: Member generates a Balanced negotiation kit for a lease
- **WHEN** a member uploads a lease PDF with found red flags and taps `[Generate Redline Kit]` then `Balanced`
- **THEN** the Advocate Sprite produces a tracked-changes `.docx` and annotated `.pdf` addressing the found flags plus a balanced email to the landlord, delivers them in Telegram, and deletes the temporary contract files

#### Scenario: No actionable red flags found
- **WHEN** a checked contract has zero found red flags
- **THEN** the `[Generate Redline Kit]` button is not offered

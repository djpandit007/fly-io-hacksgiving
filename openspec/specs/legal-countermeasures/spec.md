# legal-countermeasures Specification

## Purpose

Turns passive legal alerts and verdict cards into closed-loop member-approved counter-measures: formal opt-out and statutory rights notices with proof receipts, plus tracked-changes redlined contracts and negotiation emails generated on the Advocate Sprite.

## Requirements

### Requirement: Opt-out and rights notice extraction and drafting
The system SHALL, when a material terms change or checked document includes an actionable opt-out or statutory right (such as arbitration opt-out, AI-training opt-out, or GDPR/CCPA deletion), extract the target email address or web-form URL, deadline, and required identifying fields, and offer a draft notice to the member.

#### Scenario: Material arbitration change includes an opt-out email
- **WHEN** a watched service adds binding arbitration with a 30-day opt-out by email to `arbitration-optout@example.com`
- **THEN** the alert includes a `[Review & Draft Opt-Out]` button that previews the formatted legal notice citing the clause and target address

#### Scenario: Opt-out requires postal mail only
- **WHEN** a clause permits opting out only by physical mail
- **THEN** the system drafts the printable notice text and clearly states that electronic dispatch is not available

#### Scenario: Opt-out is an account setting
- **WHEN** a checked privacy or AI policy says the person opts out by changing a setting in their account
- **THEN** the card shows a "How to opt out" section with the verified instruction and where the setting is, says there is nothing to send, and offers no dispatch

#### Scenario: Policy gives no way to opt out
- **WHEN** a checked privacy or AI policy has an AI-training or deletion red flag but states no way to opt out
- **THEN** the card says no way to opt out could be found in the policy

#### Scenario: Policy published on a page users write
- **WHEN** the checked policy is on a shared host where users publish content (such as a GitHub repository or a hosted document)
- **THEN** the card explains how to opt out but offers no dispatch, because the text is not the service's own

#### Scenario: Member has no linked email
- **WHEN** a member without a linked email drafts an opt-out notice
- **THEN** the member is asked once for the email on their account with the service, and the notice includes it so the service can match it to the account

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

#### Scenario: Form still shown or rejected after submitting
- **WHEN** the submitted form is still on the page or shows field errors afterwards
- **THEN** the receipt is marked unconfirmed and the member is told it may have gone through and to check before sending it again

#### Scenario: Worker fails after the approval is used
- **WHEN** the Advocate worker fails after an approval token was consumed, so the notice may already have gone out
- **THEN** an unconfirmed receipt is recorded, the member is told the outcome is unknown, and the same notice is not sent again from Fine Print for 24 hours

### Requirement: Daily limits on counter-measures
The system SHALL limit counter-measures per day: at most 5 opt-out notices per member, one per member to the same address or form host, 3 to the same address or form host from all members together, and 5 redline kits per member. The opt-out limits SHALL be checked and the receipt written under one lock, so concurrent approvals cannot all pass them.

#### Scenario: Member over a daily opt-out limit
- **WHEN** an approval would exceed one of the opt-out limits
- **THEN** the draft is withdrawn, nothing is sent, and the member is told which limit applies

#### Scenario: Two approvals at once
- **WHEN** two approvals for the same target arrive together with one send left for that target today
- **THEN** exactly one notice is sent

#### Scenario: Member over the daily redline kit limit
- **WHEN** a member who has had 5 redline kits today taps a tone
- **THEN** no Advocate job runs, the held contract is cleared from memory, and the member is told to try tomorrow

### Requirement: Automated redlined contract and negotiation kit generation
The system SHALL allow a member checking a personal contract (`residential_lease`, `employment_or_nda`, or `freelance_or_saas`) to request a negotiation kit (`Polite`, `Balanced`, or `Firm`) that produces a tracked-changes `.docx`, an annotated `.pdf`, and a ready-to-send counter-proposal email via the Advocate Sprite, without persisting the uploaded contract after delivery.

#### Scenario: Member generates a Balanced negotiation kit for a lease
- **WHEN** a member uploads a lease PDF with found red flags and taps `[Generate Redline Kit]` then `Balanced`
- **THEN** the Advocate Sprite produces a tracked-changes `.docx` and annotated `.pdf` addressing the found flags plus a balanced email to the landlord, delivers them in Telegram, and deletes the temporary contract files

#### Scenario: Only unclear red flags found
- **WHEN** a checked personal contract has no red flag above the "found" threshold but some flags are unclear
- **THEN** the card says no clear red flags were found but some clauses are unclear, and still offers `[Generate Redline Kit]` for those clauses

#### Scenario: No actionable red flags found
- **WHEN** a checked contract has no found or unclear red flags
- **THEN** the `[Generate Redline Kit]` button is not offered and the card says why

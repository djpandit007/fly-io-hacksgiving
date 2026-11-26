# deadline-ledger Specification

## Purpose

Keeps each member's dated obligations (opt-out windows, renewals, trials, notice periods) in a private
ledger, reminds them before each one, and publishes them as a private calendar feed.

## Requirements

### Requirement: Per-member deadlines from terms changes
The system SHALL, when a material change opens an opt-out window with a stated number of days, add that
deadline to the ledger of each member who watches the service.

#### Scenario: Opt-out window for two watchers
- **WHEN** a change to service X opens a 30-day opt-out window and members A and B watch X
- **THEN** A's and B's ledgers each contain the opt-out deadline, and no other member's does

### Requirement: Per-member deadlines from documents
The system SHALL add dated obligations extracted from a document a member sends (via Telegram or email)
to that member's ledger only, with the source page when known.

#### Scenario: Lease sent by a member
- **WHEN** member A sends a lease that requires notice 60 days before the term ends
- **THEN** A's ledger contains the notice deadline and B's does not

### Requirement: No duplicate deadlines per member
The system SHALL keep at most one deadline per member with the same title and date.

#### Scenario: Same document sent twice
- **WHEN** a member sends the same document twice
- **THEN** each deadline appears once in that member's ledger

### Requirement: Reminders
The system SHALL remind each member, during the daily check, of their own deadlines due within the next
three days, once per deadline.

#### Scenario: Deadline in two days
- **WHEN** the daily check runs and member A has a deadline due in two days that was not yet reminded
- **THEN** A receives one reminder and is not reminded again for it

### Requirement: Private calendar feed per member
The system SHALL give each member a calendar feed URL with a token unique to them, and the feed SHALL
contain only that member's deadlines. A request without a valid token SHALL be rejected.

#### Scenario: Member opens their feed
- **WHEN** member A's feed URL is requested
- **THEN** the response is an iCalendar document containing only A's deadlines

#### Scenario: Invalid token
- **WHEN** the feed is requested with a missing or unknown token
- **THEN** the request is rejected as unauthorized

#### Scenario: Legacy owner token
- **WHEN** the feed is requested with the calendar token configured for the single-owner version
- **THEN** the response contains the owner's deadlines

### Requirement: Showing deadlines in chat
The system SHALL reply to `/deadlines` with the sender's upcoming deadlines and their own calendar feed URL.

#### Scenario: Member asks for deadlines
- **WHEN** a member sends `/deadlines`
- **THEN** they receive their own upcoming deadlines and their personal feed URL

# terms-watch Specification

## Purpose

Lets each member watch the terms of the services they use and be told about material changes, while every
service's terms are fetched, compared and judged only once no matter how many members watch it.

## Requirements

### Requirement: Per-member watchlist
The system SHALL keep a watchlist per member. `/watch` SHALL add a service to the sender's watchlist only,
and `/list` SHALL show only the sender's services with their community grade when known.

#### Scenario: Watching a service
- **WHEN** a member sends `/watch youtube.com`
- **THEN** YouTube is on that member's watchlist and on no other member's

### Requirement: Shared judging
The system SHALL fetch, compare and judge each document in a service's policy bundle once per check, regardless of how many members watch that service.

#### Scenario: Three members watch one service
- **WHEN** three members watch the same service and any document in its policy bundle changes
- **THEN** each changed section is judged once, not three times

### Requirement: Fan-out of material changes
The system SHALL send each material change to every member who watches that service: by Telegram when
the member has a linked chat, otherwise as one email digest per run to their verified address.

#### Scenario: Daily check finds a change
- **WHEN** the daily check finds a material change to a service watched by members A and B
- **THEN** A and B each receive a report of the change, and members who do not watch the service receive nothing

#### Scenario: Email-only watcher
- **WHEN** a member without Telegram watches a service that changes materially
- **THEN** they receive the report in a single email digest for that run

### Requirement: Replay of past changes to new subscribers
The system SHALL, when a member starts watching a service that is already watched, send that member the
service's material changes from the past year without judging them again.

#### Scenario: Second member watches an existing service
- **WHEN** member A already watches X with two recorded material changes this year and member B sends `/watch X`
- **THEN** B receives both change reports promptly and no new judgment is made

### Requirement: Concurrent first watch
The system SHALL backfill a newly watched service's history only once, even when several members start
watching it at the same time.

#### Scenario: Two members add the same new service together
- **WHEN** two members send `/watch` for the same unwatched service at the same time
- **THEN** its history is fetched and judged once and both members receive the reports

### Requirement: Precedents limited to the member's services
The system SHALL, when it reports that similar wording was already accepted elsewhere, name only services
on the receiving member's own watchlist.

#### Scenario: Similar clause on another member's service
- **WHEN** a change resembles a clause from a service that only another member watches
- **THEN** the report does not mention that service

### Requirement: Watching continues while anyone watches
The system SHALL check a service during the daily check if at least one active member watches it, and
SHALL stop checking it when no active member does.

#### Scenario: Last watcher removed
- **WHEN** the only member watching a service is removed
- **THEN** the service is not checked in later daily checks

### Requirement: Unwatching a service
The system SHALL let a member stop watching a service from:
- an unwatch button for each service under `/list`;
- an Unwatch button on change alerts;
- an Unwatch button on `/watch` results.

Unwatching SHALL remove the service from that member's watchlist only. The member SHALL get no further
reports for it. The service's stored history SHALL be kept.

#### Scenario: Member unwatches from /list
- **WHEN** member A and member B watch Notion and A taps unwatch for Notion under `/list`
- **THEN** Notion is no longer on A's watchlist, it is still on B's, and A gets no later Notion alerts

#### Scenario: Last watcher unwatches
- **WHEN** the only member watching a service unwatches it
- **THEN** the service is not checked in later daily checks, and its stored history remains

#### Scenario: Unwatching twice
- **WHEN** a member taps Unwatch on a service they no longer watch
- **THEN** nothing changes and they are told they had already stopped watching it

#### Scenario: Unwatch for a service the member never watched
- **WHEN** a tap asks to unwatch a service that is not on the member's watchlist
- **THEN** no member's watchlist changes

### Requirement: Undoing an unwatch
The system SHALL offer Undo after an unwatch. Undo SHALL put the service back on the member's watchlist
without fetching, judging or replaying past changes again. Undo SHALL restore only a service that this
member unwatched.

#### Scenario: Member undoes
- **WHEN** a member unwatches Notion and taps Undo
- **THEN** Notion is back on their watchlist, no provider is called, and no past change reports are sent again

#### Scenario: Undo for a service that no longer exists
- **WHEN** a member taps Undo for a service id that is not in the database
- **THEN** nothing changes and the member is told it can't be restored

#### Scenario: Forged undo for another member's service
- **WHEN** member A sends an Undo tap for a service only member B watches or unwatched
- **THEN** A's watchlist is unchanged and A is told it can't be restored

### Requirement: Multi-document Policy Bundle per service
The system SHALL track a Policy Bundle per watched service supporting distinct document roles (`terms`, `privacy`, `ai_terms`, `billing`, `dpa`), backfilling and diffing each discovered document independently while keeping backward compatibility with services that currently have only a primary terms document.

#### Scenario: Watching a service with multiple legal pages
- **WHEN** a member watches a service whose Policy Bundle includes both Terms of Service and a Privacy Policy
- **THEN** both documents are stored, diffed on daily checks, and labelled with their document role in change alerts

#### Scenario: Service with only a single terms URL
- **WHEN** discovery finds only a primary terms page for a service
- **THEN** the service is watched with its `terms` document and operates without error

### Requirement: Version history kept per document
The system SHALL keep each document's version history separately, treating a stored version as a duplicate only when the same document had the same text at the same time, so that a document returning to earlier wording and two documents with identical text are both recorded.

#### Scenario: A service walks back a change
- **WHEN** a watched document changes a clause and a later check finds the earlier wording restored
- **THEN** the restored wording is stored as a new version, judged against the changed wording, and alerted like any other material change

#### Scenario: Two documents with identical text
- **WHEN** two documents in a service's bundle serve the same text
- **THEN** each document keeps its own baseline and history

### Requirement: Companion documents looked for again periodically
The system SHALL look for companion documents again for every watched service at most every 30 days, including services watched before Policy Bundles existed, and SHALL record a document found this way with a live baseline only: no history backfill and no alert about anything before it was found.

#### Scenario: Service watched before Policy Bundles gains its AI Terms
- **WHEN** the daily run checks a service that has only its terms document and has never had a companion check
- **THEN** discovered companion documents are attached with a live baseline, no watcher is alerted about them, and later changes to them are alerted as usual

#### Scenario: Companion check not yet due
- **WHEN** a service's companions were looked for less than 30 days ago
- **THEN** the daily run checks only the documents already in its bundle

### Requirement: Cross-document contradiction detection
The system SHALL compare newly updated sections in a service's Policy Bundle against the other active documents in that same service's bundle and alert watchers when Jev detects a material contradiction between them.

#### Scenario: AI addendum contradicts Terms of Service
- **WHEN** a service's Terms of Service state that user content is never used for training, while a new AI Terms document claims a broad model-training licence
- **THEN** the system records a shared contradiction finding and delivers a contradiction alert citing both documents and their verbatim clauses to every watcher

### Requirement: Questions answered across the full Policy Bundle
The system SHALL allow `/ask` and free-text questions to search and cite verified clauses across every active document in each watched service's Policy Bundle (`terms`, `privacy`, `ai_terms`, `billing`, `dpa`), attributing each quote to its service, document role, section heading, and date.

#### Scenario: Member asks a privacy or AI question about a watched service
- **WHEN** a member asks whether a watched service uses their data to train AI models and the answer sits in that service's Privacy Policy or AI Terms
- **THEN** the system answers from the verified clauses in that document and labels the quote with the document role

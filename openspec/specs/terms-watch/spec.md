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
The system SHALL fetch, compare and judge each service's terms once per check, regardless of how many
members watch it.

#### Scenario: Three members watch one service
- **WHEN** three members watch the same service and its terms change
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

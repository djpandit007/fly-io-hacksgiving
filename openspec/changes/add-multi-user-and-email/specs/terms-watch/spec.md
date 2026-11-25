# Spec Delta

## Purpose

Lets each member watch the terms of the services they use and be told about material changes, while every
service's terms are fetched, compared and judged only once no matter how many members watch it.

## ADDED Requirements

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
The system SHALL send each material change to every member who watches that service, on each channel
the member has (Telegram and/or a verified email address).

#### Scenario: Daily check finds a change
- **WHEN** the daily check finds a material change to a service watched by members A and B
- **THEN** A and B each receive a report of the change, and members who do not watch the service receive nothing

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

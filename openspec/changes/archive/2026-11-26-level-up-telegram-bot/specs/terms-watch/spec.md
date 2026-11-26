# Spec Delta

## ADDED Requirements

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

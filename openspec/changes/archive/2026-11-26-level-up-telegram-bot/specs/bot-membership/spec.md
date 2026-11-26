# Spec Delta

## ADDED Requirements

### Requirement: Button taps follow membership
The system SHALL serve a button tap only from the owner or an active member. A tap from anyone else SHALL be
answered with a short notice and SHALL cause no other work. A removed member's taps SHALL be treated the
same way, including taps on buttons sent before they were removed.

#### Scenario: Removed member taps an old button
- **WHEN** a removed member taps Unwatch on an alert they received while a member
- **THEN** they are told the bot is private, and no watchlist changes

### Requirement: Button taps cannot reach another member's data
The system SHALL treat a tap's button data as untrusted input. It SHALL check, at the moment of the tap,
that whatever the tap acts on belongs to the tapping member. Owner-only actions SHALL be checked against
the owner.

#### Scenario: Forged unwatch
- **WHEN** member A sends a tap asking to unwatch a service only member B watches
- **THEN** B's watchlist is unchanged and A is told nothing was changed

#### Scenario: Unknown button data
- **WHEN** a tap carries data the bot doesn't recognise
- **THEN** the tap is answered with a short notice and nothing else happens

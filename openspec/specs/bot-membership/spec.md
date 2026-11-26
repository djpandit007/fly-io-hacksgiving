# bot-membership Specification

## Purpose

Decides who may use the Fine Print bot: the single owner, members who joined with the shared join code,
and email-only members, and keeps each member's data private to them.

## Requirements

### Requirement: Owner claim
The system SHALL make a chat the owner when it sends `/start <owner claim code>` while the bot has no owner,
comparing the code in constant time. An owner configured by chat id SHALL take precedence over a claimed one.

#### Scenario: First claim with the right code
- **WHEN** an unowned bot receives `/start <owner claim code>` from chat A
- **THEN** chat A becomes the owner and receives the owner help text

#### Scenario: Claim when already owned
- **WHEN** an owned bot receives `/start <owner claim code>` from another chat B
- **THEN** chat B does not become the owner and is told the bot already has an owner

#### Scenario: Configured owner
- **WHEN** an owner chat id is configured
- **THEN** that chat is treated as the owner regardless of any stored claim

### Requirement: Joining with the join code
The system SHALL make a chat a member when it sends `/start <join code>` and a join code is configured,
comparing the code in constant time. Joining SHALL be idempotent.

#### Scenario: Join with the right code
- **WHEN** chat C sends `/start <join code>`
- **THEN** chat C becomes a member and receives a welcome that explains the bot, the forwarding email address, that documents are processed by AI providers, and that it gives information, not legal advice

#### Scenario: Wrong or missing code
- **WHEN** chat D sends `/start` or `/start <wrong code>`
- **THEN** chat D is not a member and is told the bot is private and needs a join code

#### Scenario: Join code not configured
- **WHEN** no join code is configured and chat D sends `/start <anything>` that is not the owner claim code
- **THEN** chat D is not a member

#### Scenario: Joining twice
- **WHEN** an existing member sends `/start <join code>` again
- **THEN** the member's existing data is unchanged

### Requirement: Non-members are not served
The system SHALL ignore every message from a chat that is neither the owner nor an active member, except
for `/start` replies defined above.

#### Scenario: Stranger sends a link
- **WHEN** a non-member chat sends a URL, file or command other than `/start`
- **THEN** the system sends no reply and does no work for it

### Requirement: Owner-only member management
The system SHALL let the owner list members and remove a member; members SHALL NOT be able to use these
commands. A removed member SHALL be treated as a non-member until they join again.

#### Scenario: Listing members
- **WHEN** the owner sends `/members`
- **THEN** the owner receives each member's display name, chat id or email, and join date

#### Scenario: Removing a member
- **WHEN** the owner sends `/remove <member id>`
- **THEN** that member's later messages are not served

#### Scenario: Member tries an owner command
- **WHEN** a member sends `/members` or `/remove`
- **THEN** the member is told the command is owner-only and nothing changes

### Requirement: Member data isolation
The system SHALL show each member only their own watchlist, deadlines, calendar feed, Wrapped summary and
precedents, and SHALL NOT reveal one member's data to another.

#### Scenario: Two members watch different services
- **WHEN** member A watches service X and member B watches service Y, and A sends `/list`
- **THEN** A sees only X

#### Scenario: Deadlines are private
- **WHEN** member A has a deadline and member B sends `/deadlines`
- **THEN** B does not see A's deadline

### Requirement: Existing single-owner data is preserved
The system SHALL adopt services watched and deadlines recorded before multi-user support as the owner's
when the owner next claims or is configured.

#### Scenario: Upgrade from the single-owner version
- **WHEN** a database from the single-owner version with one watched service is upgraded and the owner claims the bot
- **THEN** the owner's `/list` shows that service

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

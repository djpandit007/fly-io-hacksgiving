# Spec Delta

## Purpose

Shows a member that the bot has their message and is working on it, and delivers the finished answer so
that it notifies them and is clearly tied to what they sent.

## ADDED Requirements

### Requirement: Immediate acknowledgement
The system SHALL put a 👀 reaction on a member's message as soon as it starts work that calls an outside
provider. That covers link checks, file checks, `/watch`, `/wrapped`, and taps that start such work.

#### Scenario: Member sends a link
- **WHEN** a member sends a link to check
- **THEN** their message shows a 👀 reaction before any result arrives

### Requirement: Working indicator
The system SHALL show a temporary "Thinking…" draft while the work runs. The draft SHALL name the current
stage when one is known. It SHALL keep refreshing until the work ends, including work that takes longer
than Telegram keeps a draft. If drafts cannot be shown, the system SHALL show Telegram's typing status
instead, repeated until the work ends.

#### Scenario: A long document check
- **WHEN** a member sends a PDF that takes 60 seconds to check
- **THEN** the chat shows a working indicator for the whole 60 seconds

#### Scenario: Drafts are refused
- **WHEN** Telegram returns an error for the draft
- **THEN** the chat shows the typing status instead, and the member still receives the result

### Requirement: Result as a new threaded message
The system SHALL deliver the result as a new message that replies to the member's message, so that it
notifies them. The working indicator SHALL leave no message behind in the chat.

#### Scenario: Result arrives
- **WHEN** a link check finishes
- **THEN** the member receives the verdict as a reply to the link they sent, and no "Checking…" message remains in the chat

### Requirement: Outcome on the member's message
The system SHALL replace the 👀 reaction with 👌 when the work succeeds and with 😢 when it fails. (Telegram's
reaction set has no ✅.) On failure
the system SHALL still send the error reply it sends today.

#### Scenario: Work fails
- **WHEN** checking a link raises an error
- **THEN** the member's message shows 😢 and the member receives the error reply

### Requirement: Progress never changes the outcome
A failure to set a reaction, a draft or a typing status SHALL NOT stop the work. It SHALL NOT change the
reply the member receives.

#### Scenario: Reactions are unavailable
- **WHEN** setting the reaction fails
- **THEN** the work continues and the member receives the same result

# bot-guidance Specification

## Purpose

Tells people what the Fine Print bot does and what to do next: before they join, when they look for
commands, and when they send something the bot doesn't understand.

## Requirements

### Requirement: Description before joining
The system SHALL publish a bot description and short description. They SHALL say what Fine Print does, say
that the bot is private, and tell a stranger to ask the operator for a join link.

#### Scenario: Stranger opens the bot
- **WHEN** someone opens a chat with the bot before sending anything
- **THEN** Telegram shows the description, which says the bot is private and how to get access

### Requirement: Command menu matches the chat
The system SHALL publish a command menu of the member commands (including `/jurisdiction` and `/redline`) for all private chats. It SHALL also publish a menu for the owner's chat that adds the owner-only commands. The owner's menu SHALL be republished when ownership is claimed.

#### Scenario: Member opens the menu
- **WHEN** a member types `/` in the chat
- **THEN** the menu lists the member commands including `/jurisdiction` and `/redline`, and not `/members`, `/remove` or `/stats`

#### Scenario: Owner opens the menu
- **WHEN** the owner types `/` in the chat
- **THEN** the menu also lists `/members`, `/remove` and `/stats`

#### Scenario: Owner claims the bot
- **WHEN** a chat claims ownership with the owner claim code
- **THEN** that chat's menu includes the owner-only commands without a restart

### Requirement: One-tap join link
The system SHALL accept a Telegram start link carrying the join code (`t.me/<bot>?start=<code>`) exactly as
it accepts `/start <code>`. The README SHALL describe sharing the link.

#### Scenario: Joining from a link
- **WHEN** a stranger opens the join link and presses Start
- **THEN** they become a member and receive the welcome and help

### Requirement: Help with action buttons
The system SHALL show buttons with help and with the welcome: watch a service, my list, deadlines, and how checks work. Pressing a button SHALL do the same as the matching command.

#### Scenario: Member taps My list
- **WHEN** a member taps "My list" under the help message
- **THEN** they receive the same reply as `/list`

#### Scenario: Member taps How checks work
- **WHEN** a member taps "How checks work"
- **THEN** they receive a short explanation that sending a link, PDF, screenshot or photo gets a domain-aware "before you agree" check with optional redline kits

### Requirement: Asking which service to watch
The system SHALL answer "Watch a service" and a bare `/watch` with a "Which service?" prompt. The prompt
SHALL open a reply field with an example placeholder. A member's reply to that prompt SHALL be handled as
`/watch <reply>`.

#### Scenario: Member answers the prompt
- **WHEN** a member sends `/watch` and then replies "notion" to the prompt
- **THEN** the bot watches Notion for that member as if they had sent `/watch notion`

#### Scenario: Member ignores the prompt
- **WHEN** a member sends `/watch` and then sends "notion" as a new message, not as a reply
- **THEN** the message is treated as free text

### Requirement: Suggestions for free text
The system SHALL answer free text that looks like one service name or domain with "Watch <name>?" and Yes
and No buttons. Text that looks like a service name is a single token of letters, digits, dots or hyphens.
Other free text that is a question (it contains "?" or starts with a question word such as "what", "can"
or "what's") SHALL be answered from the terms the member watches, as if sent with `/ask`. Any other
free text SHALL get a short pointer to the help buttons, which mentions `/ask`, instead of the full help.

#### Scenario: Member types a service name
- **WHEN** a member sends "notion"
- **THEN** the bot asks "Watch Notion?" with Yes and No buttons

#### Scenario: Member confirms the suggestion
- **WHEN** the member taps Yes on "Watch Notion?"
- **THEN** the bot watches Notion as if they had sent `/watch notion`, and the Yes and No buttons are removed

#### Scenario: Name too long for a button
- **WHEN** a member sends a service name too long to fit in a button's data
- **THEN** the bot replies with the short pointer to help and no suggestion

#### Scenario: Member types a question
- **WHEN** a member sends "can spotify raise my price"
- **THEN** the bot answers it from the terms the member watches, as if they had sent `/ask can spotify raise my price`

#### Scenario: Member types a statement
- **WHEN** a member sends "my password is hunter2"
- **THEN** the bot replies with a short pointer and the help buttons, not the full help text, and the text is not sent to any AI provider

### Requirement: Strangers stay unserved
The guidance in this capability SHALL NOT change who is served. A non-member's messages other than `/start`
SHALL still get no reply.

#### Scenario: Stranger types a service name
- **WHEN** a non-member sends "notion"
- **THEN** the system sends no reply

# Spec Delta

## MODIFIED Requirements

### Requirement: Command menu matches the chat
The system SHALL publish a command menu of the member commands (including `/jurisdiction` and `/dashboard`) for all private chats. It SHALL also publish a menu for the owner's chat that adds the owner-only commands. The owner's menu SHALL be republished when ownership is claimed.

#### Scenario: Member opens the menu
- **WHEN** a member types `/` in the chat
- **THEN** the menu lists the member commands including `/jurisdiction` and `/dashboard`, and not `/members`, `/remove` or `/stats`

#### Scenario: Owner opens the menu
- **WHEN** the owner types `/` in the chat
- **THEN** the menu also lists `/members`, `/remove` and `/stats`

#### Scenario: Owner claims the bot
- **WHEN** a chat claims ownership with the owner claim code
- **THEN** that chat's menu includes the owner-only commands without a restart

### Requirement: Help with action buttons
The system SHALL show buttons with help and with the welcome: watch a service, my list, deadlines, Control Room, and how checks work. Pressing a button SHALL do the same as the matching command.

#### Scenario: Member taps My list
- **WHEN** a member taps "My list" under the help message
- **THEN** they receive the same reply as `/list`

#### Scenario: Member taps Control Room
- **WHEN** a member taps "Control Room" under the help message
- **THEN** they receive a short-lived magic link to their Control Room as if they had sent `/dashboard`

#### Scenario: Member taps How checks work
- **WHEN** a member taps "How checks work"
- **THEN** they receive a short explanation that sending a link, PDF, screenshot or photo gets a domain-aware "before you agree" check with optional redline kits

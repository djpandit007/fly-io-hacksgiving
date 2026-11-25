# Spec Delta

## Purpose

Gives every member a way to send Fine Print the fine print that arrives by email: one shared inbox that
receives forwarded notices, receipts and contracts and replies with verdicts, safely tied to members.

## ADDED Requirements

### Requirement: Linking an email address
The system SHALL let a member link an email address with `/email <address>`, by sending a six-digit code
from the Fine Print inbox to that address. The address SHALL be linked only after the member sends
`/verify <code>` with the right code within 15 minutes.

#### Scenario: Successful verification
- **WHEN** a member sends `/email me@example.com` and then `/verify` with the code that arrived
- **THEN** me@example.com is linked to that member

#### Scenario: Wrong or expired code
- **WHEN** the member sends a wrong code, or the right code after 15 minutes
- **THEN** the address is not linked and the member is told to request a new code

#### Scenario: Address already linked to someone else
- **WHEN** a member tries to link an address that is verified for another member
- **THEN** the address stays with its current member and the requester is told it is in use

### Requirement: Limits on verification codes
The system SHALL send at most one code per minute and five per day to a member, and three per day to any
address. It SHALL pause linking for 24 hours after ten wrong codes in total, a count that requesting a new
code does not reset.

#### Scenario: Asking again too soon
- **WHEN** a member sends `/email` twice within a minute
- **THEN** only one code is sent and the member is asked to wait

#### Scenario: Repeated wrong codes
- **WHEN** a member enters ten wrong codes in total, across any number of requested codes
- **THEN** linking is paused for 24 hours

### Requirement: Only verified or joining senders are served
The system SHALL act on inbound mail only from a verified member address, apart from the join and
confirmation exchange below. All other mail, and any mail the inbox provider marks as spam or failing
sender authentication, SHALL be ignored without a reply.

#### Scenario: Unknown sender
- **WHEN** mail arrives from an address that is not verified and its subject lacks the join code
- **THEN** nothing is processed and no reply is sent

#### Scenario: Mail failing sender authentication
- **WHEN** mail from a verified member's address is marked as failing sender authentication
- **THEN** it is ignored

### Requirement: Email-only members
The system SHALL answer mail whose subject contains the join code, from an unknown address, by sending that
address a confirmation code. The address SHALL become an email-only member only when a reply from it
carries the code, because a From line can be forged. The join message itself SHALL NOT be processed.

#### Scenario: Judge emails with the join code
- **WHEN** someone without Telegram emails the inbox with the join code in the subject, and then replies to the confirmation email with its code
- **THEN** they become a member, and documents they forward afterwards get verdicts by email

#### Scenario: Forged join on someone else's address
- **WHEN** mail with the join code arrives with a forged From address
- **THEN** no member is created and that address can still be linked later by its real owner

### Requirement: Processing forwarded mail
The system SHALL process each accepted message as follows:
- attachments: verdict plus deadlines;
- a link to a terms page: a verdict for that page;
- a terms-update notice from a service's domain: watch that service and check it now;
- otherwise: a verdict on the body text.

#### Scenario: Contract attached
- **WHEN** a member forwards an email with a PDF contract attached
- **THEN** they receive a verdict for the contract and its deadlines are added to their ledger

#### Scenario: Terms update notice
- **WHEN** a member forwards "We've updated our Terms" from a service's domain
- **THEN** that service is added to the member's watchlist and checked immediately, and the member is told what changed

### Requirement: Replies on the member's channels
The system SHALL send the result of processed mail to the member's Telegram chat when one is linked, and as
an email reply to the sender.

#### Scenario: Member with Telegram and email
- **WHEN** a member with a linked chat forwards an email
- **THEN** they receive the verdict in Telegram and as an email reply

### Requirement: Authentic, once-only delivery
The system SHALL accept inbound mail notifications only when their signature is valid and their timestamp
is within five minutes, and SHALL process each message at most once.

#### Scenario: Forged notification
- **WHEN** a notification arrives with an invalid signature or a stale timestamp
- **THEN** it is rejected and nothing is processed

#### Scenario: Retried notification
- **WHEN** the same message is delivered twice
- **THEN** it is processed once

### Requirement: Privacy of mail
The system SHALL store only verified addresses, message identifiers for deduplication, and extracted
deadlines, not message bodies or attachments.

#### Scenario: After processing an email
- **WHEN** a forwarded email has been processed
- **THEN** its body and attachments are not kept by the system

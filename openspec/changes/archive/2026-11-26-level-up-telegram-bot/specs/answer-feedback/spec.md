# Spec Delta

## Purpose

Lets members rate the answers Fine Print writes, so the owner can see whether those answers are valued, by
kind of answer and by prompt version, without keeping what the answers said.

## ADDED Requirements

### Requirement: Rating buttons on written answers
The system SHALL put 👍 and 👎 buttons on the last message of each written answer delivered in Telegram:
- verdict cards, including those for forwarded email;
- change alerts;
- `/watch` overviews;
- `/wrapped`.

Lists, help and command confirmations SHALL NOT carry them. Answers delivered only by email SHALL NOT be
rated.

#### Scenario: Verdict card
- **WHEN** a member receives a verdict card for a link
- **THEN** the card's last message has 👍 and 👎 buttons

#### Scenario: Watch result split over several messages
- **WHEN** a `/watch` reply is sent as three messages
- **THEN** only the third message has the rating buttons

#### Scenario: List
- **WHEN** a member sends `/list`
- **THEN** the reply has no rating buttons

### Requirement: One vote per answer
The system SHALL record one vote per answer. A later tap on the same answer SHALL replace the earlier vote.
After a vote the buttons SHALL change to show the choice was recorded.

#### Scenario: Member changes their mind
- **WHEN** a member taps 👍 and then 👎 on the same answer
- **THEN** the answer has one vote, 👎

### Requirement: Optional reason after a thumbs down
The system SHALL offer the reasons Wrong, Unclear and Not useful after a 👎. Choosing a reason SHALL be
optional, and choosing one SHALL record it with the vote. The system SHALL NOT ask for or store free-text
comments.

#### Scenario: Member gives a reason
- **WHEN** a member taps 👎 and then Unclear
- **THEN** the vote is recorded as 👎 with the reason Unclear

#### Scenario: Member gives no reason
- **WHEN** a member taps 👎 and nothing else
- **THEN** the vote is recorded as 👎 with no reason

### Requirement: Feedback stores metadata only
For each rated answer the system SHALL store only:
- the kind of answer;
- the writer model and prompt version;
- for change alerts and `/watch` overviews, the id of the public terms change it covers;
- for verdict cards, whether every clause quote passed its check;
- when it was sent;
- the vote and reason.

It SHALL NOT store the answer text, the checked URL or host, file names, or which member received or rated it.

#### Scenario: Rating a document check
- **WHEN** a member rates the verdict card for their lease
- **THEN** the stored feedback contains the kind, model, prompt version and vote, and nothing from the lease and no member id

### Requirement: Votes cannot be forged onto other answers
A rating button SHALL identify its answer by an unguessable token. A tap with an unknown token SHALL be
refused without recording anything.

#### Scenario: Forged callback
- **WHEN** a client sends a rating tap with a token that was never issued
- **THEN** nothing is recorded and the member is told the answer can't be rated

### Requirement: Feedback retention
The system SHALL keep feedback for 90 days and SHALL remove older feedback during the daily run.

#### Scenario: Old feedback is pruned
- **WHEN** a daily run happens and feedback older than 90 days exists
- **THEN** that feedback is removed and newer feedback is kept

### Requirement: Feedback never changes the outcome
A failure to record an answer or a vote SHALL NOT stop the answer from being delivered. It SHALL NOT change
what the member receives.

#### Scenario: Feedback store cannot be written
- **WHEN** recording a new answer fails while a verdict card is being sent
- **THEN** the member still receives the card

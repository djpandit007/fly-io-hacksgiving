# Spec Delta

## Purpose

Helps a member understand the red flags on a verdict card: what each flag means in plain English, and the
exact wording in their document that raised it. A quote is shown only when it really appears in the
document.

## ADDED Requirements

### Requirement: Fixed explanation per flag
The system SHALL have a fixed plain-English explanation for each red flag it checks. Each explanation SHALL
fit in a Telegram pop-up alert of 200 characters. Showing an explanation SHALL NOT call any provider and
SHALL NOT need the checked document.

#### Scenario: Every flag has an explanation
- **WHEN** the set of red flags is defined
- **THEN** each flag has an explanation of at most 200 characters

### Requirement: Explain buttons on verdict cards
A verdict card delivered in Telegram SHALL carry one "What does this mean?" button for each flag found or
unclear. Tapping the button SHALL show that flag's explanation to the member.

#### Scenario: Member taps a flag
- **WHEN** a verdict card shows arbitration as found and the member taps its button
- **THEN** the member sees the arbitration explanation, and no provider is called

#### Scenario: Tapping an old card
- **WHEN** a member taps an explain button on a card from weeks ago
- **THEN** they still see the explanation

### Requirement: Clause quotes on verdict cards
The system SHALL ask the writer to quote, for each found flag, the clause of the document that raised it.
A quote that passes the check SHALL be shown under the card in a collapsed, expandable quote. A quote that
fails the check SHALL be left out without failing the card.

#### Scenario: A real quote
- **WHEN** the writer quotes an arbitration clause that appears in the document
- **THEN** the card shows that quote in an expandable quote labelled with the flag

#### Scenario: An invented quote
- **WHEN** the writer returns a quote that does not appear in the document
- **THEN** the card is sent without that quote

### Requirement: Quote check ignores formatting differences
The system SHALL check each quote once against the full document text. Before comparing, it SHALL normalise
both texts the same way:
- collapse whitespace;
- drop Markdown markup, including emphasis markers and table pipes;
- treat curly quotes and dashes as their plain forms;
- ignore letter case.

A quote shorter than a minimum length SHALL fail the check.

#### Scenario: Line breaks and bold in the parsed PDF
- **WHEN** the parsed document has "**Binding** arbitration\napplies" and the writer quotes "binding arbitration applies"
- **THEN** the quote passes the check

#### Scenario: Curly quotes
- **WHEN** the document has “Services” and the writer quotes "Services"
- **THEN** the quote passes the check

#### Scenario: A trivially short quote
- **WHEN** the writer quotes only "the company"
- **THEN** the quote fails the check and is not shown

### Requirement: Nothing kept from the checked document
The system SHALL NOT store the checked document or its quotes after the card is sent. The explain buttons
SHALL carry only the flag's name.

#### Scenario: After the card is sent
- **WHEN** a link check's card has been delivered
- **THEN** neither the document text nor the quotes are in the database

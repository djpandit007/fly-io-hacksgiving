# Spec Delta

## MODIFIED Requirements

### Requirement: Fixed explanation per flag
The system SHALL have a fixed plain-English explanation for every red flag across all legal domain playbooks (`consumer_tos`, `privacy_and_ai`, `residential_lease`, `employment_or_nda`, and `freelance_or_saas`). Each explanation SHALL fit in a Telegram pop-up alert of 200 characters. Showing an explanation SHALL NOT call any provider and SHALL NOT need the checked document.

#### Scenario: Every flag has an explanation
- **WHEN** the red-flag playbooks across all supported legal domains are defined
- **THEN** each flag key has a unique fixed explanation of at most 200 characters

#### Scenario: Member taps a domain-specific flag button
- **WHEN** a residential lease verdict card raises `repair_cost_shifting` and the member taps its explain button
- **THEN** the member sees the fixed plain-English explanation for `repair_cost_shifting` and no provider is called

# Spec Delta

## Purpose

Classifies incoming links and documents by legal domain and evaluates them with tailored red-flag batteries, jurisdiction-aware statutory unenforceability checks, and ephemeral cross-contract conflict detection against a member's watched services.

## ADDED Requirements

### Requirement: Automatic document domain classification
The system SHALL classify every checked link or uploaded document into one of five legal domains (`consumer_tos`, `privacy_and_ai`, `residential_lease`, `employment_or_nda`, `freelance_or_saas`) before running red-flag evaluation, defaulting to `consumer_tos` when classification is unavailable or uncertain.

#### Scenario: Member sends a residential lease PDF
- **WHEN** a member uploads a residential apartment lease
- **THEN** the system classifies it as `residential_lease`, displays the detected domain on the verdict card, and runs the lease red-flag battery

#### Scenario: Member sends an employment offer and PIIA
- **WHEN** a member forwards an offer letter containing invention-assignment terms
- **THEN** the system classifies it as `employment_or_nda` and evaluates employment-specific flags such as side-project IP capture and non-compete scope

### Requirement: Domain-specific red-flag batteries
The system SHALL evaluate each classified document against the red-flag questions defined for its domain rather than only generic consumer terms flags, reporting found, unclear, and not-found status with calibrated probabilities and verified verbatim quotes.

#### Scenario: Lease-specific flags checked
- **WHEN** a document is classified as `residential_lease`
- **THEN** the verdict checks lease flags including automatic renewal traps, repair-cost shifting, early termination penalties, security deposit traps, and landlord entry notice

#### Scenario: Freelance or SaaS agreement checked
- **WHEN** a document is classified as `freelance_or_saas`
- **THEN** the verdict checks flags including uncapped indemnification, asymmetric liability limits, extended payment windows, pre-payment work-for-hire transfer, and unilateral termination without kill fee

### Requirement: Per-member legal jurisdiction setting
The system SHALL allow each member to set or clear their legal jurisdiction via `/jurisdiction` (supporting at minimum `US-CA`, `US-NY`, `US-OTHER`, `EU`, and `UK`) and SHALL store only the selected jurisdiction code on the member's profile.

#### Scenario: Member sets jurisdiction to California
- **WHEN** a member sends `/jurisdiction US-CA`
- **THEN** the system saves `US-CA` for that member only and confirms that future checks will include California statutory notes

#### Scenario: Invalid jurisdiction code
- **WHEN** a member sends `/jurisdiction MARS`
- **THEN** the system rejects the value and lists the supported jurisdiction codes

### Requirement: Jurisdiction-aware statutory unenforceability checks
The system SHALL, when a member has a configured jurisdiction, check found red flags against that jurisdiction's statutory ruleset and annotate clauses that are likely void, restricted, or carry statutory opt-out or cancellation rights, always with an "Information, not legal advice" disclaimer.

#### Scenario: Non-compete or overbroad IP assignment in California
- **WHEN** a member with jurisdiction `US-CA` checks an employment agreement containing a non-compete or off-hours side-project IP assignment
- **THEN** the verdict notes that the clause may be restricted or void under California law (e.g., Bus. & Prof. Code § 16600 or Labor Code § 2870) and includes the disclaimer

#### Scenario: Member has not set a jurisdiction
- **WHEN** a member with no configured jurisdiction checks a contract
- **THEN** the verdict omits jurisdiction-specific statutory notes and briefly mentions `/jurisdiction`

### Requirement: Ephemeral cross-contract conflict detection against watched services
The system SHALL, when a member uploads an `employment_or_nda` or `freelance_or_saas` contract, ephemerally compare its confidentiality and IP restrictions against the current policy bundles of the services on that member's watchlist, surfacing concrete conflicts without storing the uploaded contract.

#### Scenario: Uploaded NDA conflicts with a watched SaaS tool's AI terms
- **WHEN** member A watches Notion (whose AI terms allow training on workspace content) and uploads an employer NDA prohibiting third-party model training on work inputs
- **THEN** the verdict warns of the conflict naming Notion and the relevant NDA clause, and deletes the uploaded NDA text once the reply is sent

#### Scenario: Conflict check stays private to the uploading member
- **WHEN** member A uploads an NDA while member B watches Notion
- **THEN** member B receives no notification and no trace of A's NDA is stored in the database or shared index

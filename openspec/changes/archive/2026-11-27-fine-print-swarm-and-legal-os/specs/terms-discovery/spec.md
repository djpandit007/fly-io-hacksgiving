# Spec Delta

## MODIFIED Requirements

### Requirement: Clean page text when direct fetching fails
The system SHALL obtain a legal page's text via the Ephemeral Scout Sprite (using a headless browser under a per-job DNS egress lock) when a direct fetch is refused, fails, or yields too little text, falling back to the content-extraction service when the Scout Sprite is unavailable or fails. PDFs SHALL be read through document parsing.

#### Scenario: Site blocks the fetch
- **WHEN** a legal page returns an error or almost no text to a direct fetch while the Scout Sprite is available
- **THEN** the Scout Sprite renders the page under a domain-locked DNS policy, extracts clean normalised text, captures an evidentiary screenshot, and restores its base checkpoint

#### Scenario: Scout Sprite unavailable
- **WHEN** a direct fetch fails and the Scout Sprite is not configured or unreachable
- **THEN** the system falls back to the content-extraction service and continues

## ADDED Requirements

### Requirement: Policy Bundle discovery per service
The system SHALL, when resolving a service from a name, domain, or primary terms URL, discover linked companion legal documents on the service's domain (`privacy`, `ai_terms`, `billing`, `dpa`) in addition to the primary `terms` page, subject to the same public-address safety checks.

#### Scenario: Discovering companion legal policies
- **WHEN** a member sends `/watch notion` and Notion's legal hub links to Terms, Privacy Policy, and AI Terms on `notion.so`
- **THEN** the system attaches the discovered companion documents to Notion's Policy Bundle and reports which document roles are being watched

#### Scenario: Companion link points to an unsafe address
- **WHEN** a discovered companion link resolves to a non-public address
- **THEN** the link is skipped and never fetched

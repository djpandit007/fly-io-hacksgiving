# terms-discovery Specification

## Purpose

Finds the terms-of-service page for a service from just a name or domain, and gets clean readable text of
a terms page even when a direct fetch is blocked or renders nothing without JavaScript.

## Requirements

### Requirement: Finding a terms page from a name or domain
The system SHALL resolve `/watch <name or domain>` to the service's terms-of-service page using web search,
preferring pages on the service's own domain when a domain is given, and falling back to links on the
service's home page.

#### Scenario: Name only
- **WHEN** a member sends `/watch notion`
- **THEN** the system watches Notion's terms-of-service page and tells the member which URL it chose

#### Scenario: Nothing found
- **WHEN** no terms page can be found
- **THEN** the member is asked to send the full URL and nothing is added

### Requirement: Clean page text when direct fetching fails
The system SHALL obtain a legal page's text via the Ephemeral Scout Sprite (using a headless browser under a per-job DNS egress lock) when a direct fetch is refused, fails, or yields too little text, falling back to the content-extraction service when the Scout Sprite is unavailable or fails. PDFs SHALL be read through document parsing.

#### Scenario: Site blocks the fetch
- **WHEN** a legal page returns an error or almost no text to a direct fetch while the Scout Sprite is available
- **THEN** the Scout Sprite renders the page under a domain-locked DNS policy, extracts clean normalised text, captures an evidentiary screenshot, and restores its base checkpoint

#### Scenario: Scout Sprite unavailable
- **WHEN** a direct fetch fails and the Scout Sprite is not configured or unreachable
- **THEN** the system falls back to the content-extraction service and continues

### Requirement: Fetch safety is preserved
The system SHALL keep refusing direct fetches of non-public addresses for any URL, including URLs returned
by search.

#### Scenario: Search returns an internal address
- **WHEN** a discovered or forwarded URL resolves to a private or loopback address
- **THEN** the system does not fetch it directly

### Requirement: Policy Bundle discovery per service
The system SHALL, when resolving a service from a name, domain, or primary terms URL, discover linked companion legal documents on the service's domain (`privacy`, `ai_terms`, `billing`, `dpa`) in addition to the primary `terms` page, subject to the same public-address safety checks.

#### Scenario: Discovering companion legal policies
- **WHEN** a member sends `/watch notion` and Notion's legal hub links to Terms, Privacy Policy, and AI Terms on `notion.so`
- **THEN** the system attaches the discovered companion documents to Notion's Policy Bundle and reports which document roles are being watched

#### Scenario: Companion link points to an unsafe address
- **WHEN** a discovered companion link resolves to a non-public address
- **THEN** the link is skipped and never fetched

#### Scenario: Terms page needs a browser
- **WHEN** a service's terms page can be read only by the Scout, at first watch or at a periodic companion check
- **THEN** the policy links the Scout saw on that page are considered as companions, ignoring the Scout's own labels and applying the same domain, user-content, path and public-address checks, and links on another site, on user-written pages, or not recognised as policies are dropped

#### Scenario: Link to a page users write on a shared host
- **WHEN** a link points to a shared host where users publish content (such as a GitHub repository or a hosted document), including through a trailing-dot host name, dot segments, backslashes or encoded separators in its path
- **THEN** it is treated as user content and never attached as a companion document

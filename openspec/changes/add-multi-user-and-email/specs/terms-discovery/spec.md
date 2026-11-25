# Spec Delta

## Purpose

Finds the terms-of-service page for a service from just a name or domain, and gets clean readable text of
a terms page even when a direct fetch is blocked or renders nothing without JavaScript.

## ADDED Requirements

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
The system SHALL obtain a terms page's text from a content-extraction service when a direct fetch is
refused, fails, or yields too little text. PDFs SHALL be read through document parsing.

#### Scenario: Site blocks the fetch
- **WHEN** a terms page returns an error or almost no text to a direct fetch
- **THEN** the system still produces the page's text from the extraction service and continues

### Requirement: Fetch safety is preserved
The system SHALL keep refusing direct fetches of non-public addresses for any URL, including URLs returned
by search.

#### Scenario: Search returns an internal address
- **WHEN** a discovered or forwarded URL resolves to a private or loopback address
- **THEN** the system does not fetch it directly

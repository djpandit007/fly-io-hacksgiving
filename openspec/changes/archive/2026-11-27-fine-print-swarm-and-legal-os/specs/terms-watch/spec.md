# Spec Delta

## MODIFIED Requirements

### Requirement: Shared judging
The system SHALL fetch, compare and judge each document in a service's policy bundle once per check, regardless of how many members watch that service.

#### Scenario: Three members watch one service
- **WHEN** three members watch the same service and any document in its policy bundle changes
- **THEN** each changed section is judged once, not three times

## ADDED Requirements

### Requirement: Multi-document Policy Bundle per service
The system SHALL track a Policy Bundle per watched service supporting distinct document roles (`terms`, `privacy`, `ai_terms`, `billing`, `dpa`), backfilling and diffing each discovered document independently while keeping backward compatibility with services that currently have only a primary terms document.

#### Scenario: Watching a service with multiple legal pages
- **WHEN** a member watches a service whose Policy Bundle includes both Terms of Service and a Privacy Policy
- **THEN** both documents are stored, diffed on daily checks, and labelled with their document role in change alerts

#### Scenario: Service with only a single terms URL
- **WHEN** discovery finds only a primary terms page for a service
- **THEN** the service is watched with its `terms` document and operates without error

### Requirement: Cross-document contradiction detection
The system SHALL compare newly updated sections in a service's Policy Bundle against the other active documents in that same service's bundle and alert watchers when Jev detects a material contradiction between them.

#### Scenario: AI addendum contradicts Terms of Service
- **WHEN** a service's Terms of Service state that user content is never used for training, while a new AI Terms document claims a broad model-training licence
- **THEN** the system records a shared contradiction finding and delivers a contradiction alert citing both documents and their verbatim clauses to every watcher

### Requirement: Questions answered across the full Policy Bundle
The system SHALL allow `/ask` and free-text questions to search and cite verified clauses across every active document in each watched service's Policy Bundle (`terms`, `privacy`, `ai_terms`, `billing`, `dpa`), attributing each quote to its service, document role, section heading, and date.

#### Scenario: Member asks a privacy or AI question about a watched service
- **WHEN** a member asks whether a watched service uses their data to train AI models and the answer sits in that service's Privacy Policy or AI Terms
- **THEN** the system answers from the verified clauses in that document and labels the quote with the document role

# clause-benchmarking Specification

## Purpose

Benchmarks clauses in material changes and checked contracts against Fine Print's Pinecone corpus so members can see whether a clause is industry-standard or unusually aggressive relative to comparable services and domains.

## Requirements

### Requirement: Clause corpus indexing with severity and domain metadata
The system SHALL index public material clauses and anonymised aggregate benchmark vectors in Pinecone with metadata including document domain, category, severity score, favours direction, and opt-out availability, never indexing private uploaded contract text.

#### Scenario: Public service material change indexed
- **WHEN** a watched service's policy bundle records a material change
- **THEN** the clause vector is upserted into Pinecone with its domain, category, severity, favours, and opt-out metadata

#### Scenario: Severity measured on one scale
- **WHEN** a clause is indexed or benchmarked
- **THEN** its severity is the clause's own severity (how much the clause matters to the person agreeing), never the severity of the change that introduced it, and benchmarks compare only indexed clauses scored on that same scale

#### Scenario: Personal contract checked
- **WHEN** a member checks a private lease or employment offer
- **THEN** its clauses are queried against Pinecone for benchmarking only and are never upserted into the index

### Requirement: Market percentile scoring for comparable clauses
The system SHALL compute a market percentile rank and short peer comparison for a clause whenever at least a minimum cohort of comparable public clauses exists in Pinecone for the same category, omitting the benchmark cleanly when the cohort is too small.

#### Scenario: Aggressive arbitration clause without opt-out
- **WHEN** a service introduces an arbitration clause with no opt-out and the Pinecone cohort contains at least 10 comparable arbitration clauses
- **THEN** the report displays a grounded percentile note (for example, worse severity than N% of indexed services in that category)

#### Scenario: Insufficient benchmark cohort
- **WHEN** fewer than the minimum comparable clauses exist in Pinecone for a category
- **THEN** the report omits the percentile line rather than showing a noisy or fabricated rank

### Requirement: Member privacy preserved in peer comparisons
The system SHALL ensure benchmark peer summaries reference only public services from the shared corpus (or aggregate cohort statistics) and SHALL NEVER expose another member's private watchlist or uploaded documents.

#### Scenario: Benchmark cites comparable public services
- **WHEN** member A checks a SaaS contract and receives a benchmark comparison
- **THEN** any named comparable services come solely from the public indexed corpus and reveal nothing about who watches them

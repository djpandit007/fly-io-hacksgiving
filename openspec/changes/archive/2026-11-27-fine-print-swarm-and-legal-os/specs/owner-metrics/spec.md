# Spec Delta

## MODIFIED Requirements

### Requirement: Owner reads metrics with /stats
The system SHALL answer the owner's `/stats` command with a summary of the past 7 days: daily runs and
their failures, background job failures, per-provider calls, failures and response times, reported cost,
usage totals, answer feedback, and multi-Sprite swarm and counter-measure totals. Members who are not the
owner SHALL be told the command is owner-only.

#### Scenario: Owner asks for stats
- **WHEN** the owner sends `/stats`
- **THEN** the reply covers the past 7 days and includes the runs, their failed services, provider call and failure counts with average and slowest response times, counts of members, watched services, material changes, commands used, Scout/Advocate runs, checkpoint restore timings, and counter-measure dispatch counts

#### Scenario: A member asks for stats
- **WHEN** a member who is not the owner sends `/stats`
- **THEN** they are told the command is owner-only and receive no metrics

#### Scenario: No per-member breakdown
- **WHEN** the owner sends `/stats`
- **THEN** the reply contains totals only and does not attribute any activity to an individual member

#### Scenario: Feedback summary
- **WHEN** answers were rated during the past 7 days
- **THEN** the reply shows, for each kind of answer, the number of ratings, the share that were 👍, the 👎 reasons, and the 👍 share for each prompt version

#### Scenario: No feedback yet
- **WHEN** no answers were rated during the past 7 days
- **THEN** the reply says there is no feedback yet rather than showing 0%

## ADDED Requirements

### Requirement: Multi-Sprite swarm and counter-measure telemetry
The system SHALL count per day by outcome: Scout Sprite crawls, DNS egress locks applied, Scout base-checkpoint restores (including average and max restore duration), Advocate redline-kit generations, and approved opt-out dispatches, storing zero URLs, file names, message text, or member identifiers.

#### Scenario: Scout crawl and checkpoint restore recorded
- **WHEN** the Scout Sprite completes a crawl and restores its base checkpoint
- **THEN** the daily counts for Scout jobs and checkpoint restores increase by one with elapsed duration, and neither the crawled URL nor any member id is stored in metrics

#### Scenario: Approved opt-out dispatch recorded
- **WHEN** the Advocate Sprite dispatches a member-approved opt-out notice
- **THEN** the daily counter-measure count increases by one without storing the target email address, notice text, or member identity

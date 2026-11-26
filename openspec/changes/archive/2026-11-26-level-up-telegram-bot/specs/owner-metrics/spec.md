# Spec Delta

## ADDED Requirements

### Requirement: Button taps are counted
The system SHALL count button taps per day by action name and outcome, in the same way as commands. The
counts SHALL hold no button data, no service and no member. Rating taps SHALL NOT also be counted here,
because answer feedback records them.

#### Scenario: Member unwatches from a button
- **WHEN** a member taps Unwatch
- **THEN** the day's count for the unwatch tap rises by one, and the count names neither the service nor the member

## MODIFIED Requirements

### Requirement: Owner reads metrics with /stats
The system SHALL answer the owner's `/stats` command with a summary of the past 7 days: daily runs and
their failures, background job failures, per-provider calls, failures and response times, reported cost,
usage totals, and answer feedback. Members who are not the owner SHALL be told the command is owner-only.

#### Scenario: Owner asks for stats
- **WHEN** the owner sends `/stats`
- **THEN** the reply covers the past 7 days and includes the runs, their failed services, provider call and failure counts with average and slowest response times, and counts of members, watched services, material changes and commands used

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

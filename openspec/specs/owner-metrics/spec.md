# owner-metrics Specification

## Purpose

Lets the owner see whether Fine Print is working, how its external providers are behaving and what they
report charging, and how much the bot is used, without an external observability service.

## Requirements

### Requirement: Daily run record
The system SHALL keep a record of every daily run: when it ran, how long it took, how many services were
checked, which services failed, how many changes and nudges it produced, and how many deliveries were
sent and how many failed.

#### Scenario: A run with a failed service
- **WHEN** a daily run checks three services and one of them cannot be fetched
- **THEN** the record for that run shows two checked, names the failed service, and shows the run's duration

#### Scenario: A delivery fails
- **WHEN** a daily run has messages for two members and sending to one of them fails
- **THEN** the record shows one delivery sent and one failed, and the other member still receives their messages

### Requirement: Background job failures are recorded
The system SHALL record each Telegram message job and each forwarded-mail job that ends in an error, with
its kind, when it happened and the type of error, and SHALL record how long jobs of each kind take.

#### Scenario: A forwarded email cannot be processed
- **WHEN** processing a forwarded email raises an error
- **THEN** a mail job failure is recorded with the error type and without the email's content or sender

### Requirement: Provider calls are counted and timed
The system SHALL count calls to each external provider per day by outcome (succeeded, failed, retried) and
SHALL keep the average and the slowest response time per provider per day. Failures to send a Telegram
message SHALL be counted as provider failures.

#### Scenario: A call succeeds after one retry
- **WHEN** a provider answers with a retryable error and then succeeds
- **THEN** that provider's counts for the day show one retried attempt and one success

#### Scenario: A call fails for good
- **WHEN** a provider call ends in an error after its retries are used up
- **THEN** that provider's failed count for the day rises by one and the caller sees the same error as before

### Requirement: Cost comes from the provider
The system SHALL record cost in US dollars only from an amount the provider reported in its response.
The system SHALL NOT compute cost from a price held in configuration or code.

#### Scenario: Provider reports a dollar cost
- **WHEN** a provider's response states what the request cost in US dollars
- **THEN** that amount is added to the provider's dollar total for the day

#### Scenario: Provider reports usage but no money
- **WHEN** a provider's response reports only its own units, such as credits, read units or tokens
- **THEN** those units are added to the provider's totals for the day and nothing is added to any dollar total

#### Scenario: Response carries no cost or usage
- **WHEN** a provider's response has no cost or usage information, or it is not in the expected form
- **THEN** nothing is recorded for cost, the call itself is unaffected, and the provider is later shown as not reported rather than as zero

### Requirement: Command use is counted
The system SHALL count, per day, how many times each bot command was used and how many of those ended in
an error. A document or link sent for checking SHALL be counted as its own kind of use.

#### Scenario: A member watches a service
- **WHEN** a member sends `/watch notion`
- **THEN** the day's count for the watch command rises by one

#### Scenario: A member sends a PDF
- **WHEN** a member sends a PDF to be checked
- **THEN** the day's count of document checks rises by one

### Requirement: Metrics hold no member content
The system SHALL store only counts, durations, amounts, error types, provider names, command names and
the names of services that failed in a run. It SHALL NOT store URLs, file names, message or email text,
email addresses, or which member did what.

#### Scenario: Recording a link check
- **WHEN** a member sends a link to be checked
- **THEN** the metrics record that a link check happened and contain neither the link nor the member

### Requirement: Recording never changes the outcome of the work
A failure to record a metric SHALL NOT cause a daily run, a background job, a bot reply or a provider call
to fail or to return a different result.

#### Scenario: The metrics store cannot be written
- **WHEN** writing a metric fails while a member's command is being handled
- **THEN** the member receives the same reply they would have received otherwise

### Requirement: Owner reads metrics with /stats
The system SHALL answer the owner's `/stats` command with a summary of the past 7 days: daily runs and
their failures, background job failures, per-provider calls, failures and response times, reported cost,
and usage totals. Members who are not the owner SHALL be told the command is owner-only.

#### Scenario: Owner asks for stats
- **WHEN** the owner sends `/stats`
- **THEN** the reply covers the past 7 days and includes the runs, their failed services, provider call and failure counts with average and slowest response times, and counts of members, watched services, material changes and commands used

#### Scenario: A member asks for stats
- **WHEN** a member who is not the owner sends `/stats`
- **THEN** they are told the command is owner-only and receive no metrics

#### Scenario: No per-member breakdown
- **WHEN** the owner sends `/stats`
- **THEN** the reply contains totals only and does not attribute any activity to an individual member

### Requirement: Cost is reported honestly in /stats
The `/stats` reply SHALL show the dollar total of provider-reported cost,
SHALL list usage in other units beside it as not included in the total, and SHALL state that the cost of
running the Sprite is not included.

#### Scenario: Mixed units
- **WHEN** one provider reported dollars and another reported only credits during the past 7 days
- **THEN** the reply shows the dollar total, shows the credits separately, and says the credits are not part of the total

### Requirement: Daily run reports to the scheduler
The scheduled daily run SHALL publish that run's summary where the owner can read it in the scheduler,
and SHALL be marked as failed when any service failed to be checked or the run could not be completed.

#### Scenario: A service failed
- **WHEN** the daily run completes with one failed service
- **THEN** the scheduled job shows the summary and ends as failed

#### Scenario: A clean run
- **WHEN** the daily run completes with no failed service
- **THEN** the scheduled job shows the summary and ends as successful

#### Scenario: The app cannot be reached
- **WHEN** the scheduled job cannot reach the app or receives an error
- **THEN** the scheduled job ends as failed

### Requirement: Metrics retention
The system SHALL keep metrics for 90 days and SHALL remove older metrics during the daily run.

#### Scenario: Old metrics are pruned
- **WHEN** a daily run happens and metrics older than 90 days exist
- **THEN** those older metrics are removed and newer ones are kept

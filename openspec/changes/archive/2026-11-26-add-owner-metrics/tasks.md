# Tasks

## 1. Confirm provider cost fields with live calls

- [x] 1.1 Make one Jev (System One) call and one chat call through OpenCode and record in design.md whether each response carries `usage.cost`; verify by pasting the observed `usage` keys (no content) into the design's cost table
- [x] 1.2 Make one Exa search and one contents call through the Sprite connector and record the observed `costDollars` shape in design.md; verify the table row is updated from "differs between sources" to the observed shape
- [x] 1.3 Read one completed LlamaCloud parse job with `expand=usage` through the Sprite connector and record whether `usage.credits` is returned; verify the design table row states the result
- [x] 1.4 If 1.1 shows no cost on Jev responses, request `v1/models` through the OpenCode connector and record whether prices are returned; verify design.md states whether the catalogue fallback is available, and remove the Open Questions that are now answered

## 2. Storage and recorder

- [x] 2.1 Add schema version 4 in `app/db.py` with the run log, daily counters (count, total ms, slowest ms) and daily usage (provider, unit, amount) tables; verify with a `tests/test_db.py` case that a version 3 database migrates and keeps its data
- [x] 2.2 Add `app/metrics.py` with functions to record a run, a job failure, a counted and timed event, and a usage amount, each swallowing and logging its own errors; verify with tests that counters upsert per day and that a write against a closed connection does not raise
- [x] 2.3 Add a prune function that removes metrics older than 90 days; verify with a test that seeds old and new rows

## 3. Record runs, jobs and deliveries

- [x] 3.1 Make `notify.deliver` report failed deliveries as well as sent ones and count Telegram send failures as provider failures; verify with a `tests` case where one member's send raises and the other still receives theirs
- [x] 3.2 Record each tick in `app/main.py` (duration, summary fields, failed service names, failed deliveries) and prune after it; verify with a `tests/test_main.py` case that a tick with one failing service writes one run row naming it
- [x] 3.3 Record failures and durations of Telegram and mail jobs in `_handle_message` and `_handle_mail`; verify with a test that a raising mail job writes a failure row containing the error type and no sender or content

## 4. Record provider calls, latency and cost

- [x] 4.1 Pass an optional recorder from `lifespan` through `Clients` to each `Gateway`, and count and time each attempt in `Gateway.request` by outcome; verify with a respx test that a 503 followed by 200 records one retried and one ok, and that a `Gateway` built without a recorder still works
- [x] 4.2 Read `usage.cost` in `app/writer.py` and record it in dollars; verify with a fixture response carrying a cost and one without
- [x] 4.3 Record Jev tokens in `app/judge.py`, plus dollars from `usage.cost` (confirmed present by 1.1, so no catalogue fallback); verify with fixture responses with and without a cost
- [x] 4.4 Read `costDollars` in `app/exa.py` using the shape observed in 1.2; verify with fixture responses with and without the field
- [x] 4.5 Request `expand=usage` in `app/parse.py` job reads and record credits when present; verify with a fixture where credits is a number and one where it is null
- [x] 4.6 Read `usage` on Pinecone embed and query responses in `app/memory.py` and record embedding tokens and read units; verify with fixture responses with and without `usage`

## 5. Command counts and /stats

- [x] 5.1 Count each command, document check and link check in `bot.handle`, with an error outcome from its existing `except`; verify with `tests/test_bot.py` cases that no URL, file name or member id reaches the metrics tables
- [x] 5.2 Add the owner-only `/stats` command and its 7-day report (runs, job failures, providers with average and slowest times, dollar total, other units listed as not included, Sprite cost excluded, usage totals from existing tables); verify with tests for the owner reply, the non-owner refusal, and a provider shown as "not reported"
- [x] 5.3 Add `/stats` to the owner help text in `app/bot.py` and to the README command table; verify the help test and that the README row matches the command's behaviour

## 6. Daily workflow

- [x] 6.1 Change `.github/workflows/tick.yml` to write the tick response to `$GITHUB_STEP_SUMMARY` and exit non-zero when `failed` is non-empty; verify by running the step's shell logic locally against a sample response with and without failed services

## 7. Integration check

- [x] 7.1 Run `uv run pytest` and `uv run ruff check`; verify both pass
- [x] 7.2 Deploy to the Sprite, trigger the workflow with `workflow_dispatch`, then send `/stats` as owner; verify the job summary shows the run and `/stats` shows that run, at least one provider with a response time, and a cost line

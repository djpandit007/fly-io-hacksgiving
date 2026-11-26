# Design

## Context

See proposal.md for motivation. The constraints that shape the approach:

- The app is one uvicorn worker with one event loop and one SQLite connection on the Sprite's
  persistent disk (`app/main.py`, `app/db.py`).
- The Sprite pauses about 30 seconds after the last request. Anything that polls it wakes it, and it
  cannot act on a schedule by itself; the daily run is started by `.github/workflows/tick.yml`.
- Connector providers (OpenCode, Pinecone, Exa, LlamaCloud, AgentMail) are all called through
  `Gateway.request` (`app/gateway.py`). Telegram, the Sprite task API, and plain page fetches
  (terms pages, Wayback, Open Terms Archive, ToS;DR) use their own `httpx` clients.
- `Gateway` instances are built in `Clients` (`app/agent.py`) and have no database handle.
- Traffic is tiny: one tick a day plus occasional webhooks.

```
 recording points                          store                     read
 agent.tick + notify.deliver    --+
 main._handle_message/_handle_mail --+--> metrics tables in    --> /stats (owner, Telegram)
 bot.handle (command name only)  --+      fineprint.db
 Gateway.request (via recorder)  --+
 users, subscriptions, changes, ledger -----(queried directly)----^

 /tick response (TickSummary) --> tick.yml --> job summary + pass/fail --> GitHub email
```

## Goals / Non-Goals

**Goals:**

- Answer three questions from Telegram: is it working, how are the providers doing, is anyone using it.
- Add no dependency, no second service and no always-on process.
- Recording never changes the outcome of the work it measures.

**Non-Goals:**

- Latency percentiles or graphs over time. Average and slowest per day are in scope.
- A single all-in dollar figure. Providers that do not report money are shown in their own unit.
- The Sprite's compute cost, and currency conversion.
- Recording calls that do not go through `Gateway`, other than Telegram send failures.
- Per-member activity reports.

## Decisions

### Store metrics in SQLite rather than a metrics stack

Rows in `fineprint.db` survive pauses and restarts, need no new dependency, and can be read with SQL.

Alternatives considered:

- **Prometheus `/metrics` plus a scraper:** each scrape wakes the Sprite, in-memory counters reset on
  restart, and the scraper and a dashboard need somewhere always-on to run.
- **OpenTelemetry to self-hosted Grafana or SigNoz:** a second system to operate for a few hundred
  events a day.
- **JSON log lines only:** the cheapest to add, but trends need log retention and a query tool, and
  Sprite log retention has not been verified.

### Two shapes of data

- **Run log:** one row per tick with the `TickSummary` fields plus failed deliveries and the names of
  the services that failed. One row per failed background job with its kind (telegram, mail) and the
  exception type.
- **Daily counters:** one row per (day, kind, name, outcome) with a count, upserted in place. Used for
  provider calls (name = provider, outcome = ok | error | retried) and bot commands (name = command).

A single event-per-call table was considered and rejected: a tick can make many provider calls, and
counters stay bounded at a few rows per day without a cleanup job doing real work.

### Latency as a sum and a maximum on the daily counters

Each daily counter row also carries total milliseconds and the slowest single call, so `/stats` can
show average and worst response time per provider without a row per call. `Gateway.request` times each
attempt; the tick and job handlers time themselves and store the duration on their run-log row.
Percentiles were rejected because they need a row per call or a histogram, which this traffic does not
justify.

### Cost comes from the provider, never from configuration

A third table of daily usage counters holds (day, provider, unit, amount), where unit is one of `usd`,
`input_tokens`, `output_tokens`, `credits`, `read_units`, `embed_tokens`.

| Provider | Source | Unit recorded | Observed on the Sprite, 2026-11-26 |
|---|---|---|---|
| OpenCode chat model | `usage.cost` | usd | Present. `usage` keys: `prompt_tokens`, `completion_tokens`, `total_tokens`, `prompt_tokens_details`, `completion_tokens_details`, `cost`, `opencode` |
| OpenCode Jev (System One) | `usage.cost`, `usage.input_tokens`, `usage.output_tokens` | usd, tokens | Present. `usage` keys: `input_tokens`, `output_tokens`, `cost`, `opencode` |
| Exa | `costDollars.total` on search and contents responses | usd | Present. Search: `{total, search: {neural}}`; contents: `{total, contents: {text}}` |
| LlamaCloud parse | `job.usage.credits` with `expand=usage` on the job read | credits | Present; `job.usage` is null without the parameter. Repeated `expand` parameters are accepted |
| LlamaCloud extract | top-level `usage.credits` with `expand=usage` | credits | Present, alongside `extract_credits` and `parse_credits` |
| Pinecone | `usage.readUnits` on query, `usage.total_tokens` on embed | read_units, embed_tokens | Present |
| AgentMail, Telegram | none | none | No per-call charge reported |

The generic `Gateway` cannot know these shapes, so each provider module reads its own field from the
raw response and passes the amount to the same recorder through the gateway. The read is deliberately
lenient (a missing key or a non-number records nothing) rather than part of the strict Pydantic
models, so an unexpected cost shape can never fail the call.

Jev responses carry `usage.cost`, so no dollar amount is ever derived from a price: the model-catalogue
fallback considered earlier is not built. (The catalogue is reachable through the connector and does
return prices, should a future provider need it.)

Alternatives considered:

- **A price table in settings:** rejected by the owner; it drifts silently when a provider reprices.
- **Converting credits and read units to dollars:** needs plan-specific prices that no response or
  catalogue endpoint provides, so those stay in their own units.
- **Provider billing or balance endpoints:** none found in the documentation read for OpenCode; not
  investigated for the others, and the Sprite connectors forward only selected paths.

### Usage is mostly queried, not recorded

Members joined, services watched, material changes, deadlines and mail received already exist in
`users`, `subscriptions`, `changes`, `ledger` and `mail_seen`. `/stats` queries them. Only command use
is newly recorded; a file or link sent for a "before you agree" check is counted under a fixed name
(for example `check:file`, `check:url`).

### A recorder is passed into `Clients`

`Clients` takes an optional recorder callable and hands it to each `Gateway`; `Gateway.request` calls
it once per attempt outcome. `lifespan` builds the recorder from the open connection. Tests and local
scripts that build a `Gateway` without one keep working.

Alternative considered: a module-level global connection. Rejected because the codebase passes `conn`
explicitly everywhere and tests rely on that.

### Command outcomes are recorded where they are already caught

`bot.handle` already catches and reports command errors, so the command counter and its error outcome
are recorded there. `_handle_message` and `_handle_mail` in `app/main.py` record only what escapes
that: a failure outside `bot.handle`, or a mail job that raised.

### GitHub Actions is the signal for the daily run

The workflow already receives `TickSummary` as the response body. It writes it to
`$GITHUB_STEP_SUMMARY` and exits non-zero when `failed` is non-empty. `curl -f` already fails the job
when the Sprite is unreachable or returns an error. A sleeping Sprite cannot report a tick that never
ran, so this cannot be done from inside the app.

### Defaults accepted by the owner

These came out of exploration as recommendations and were accepted when the change was reviewed:

- **Counts only.** No URLs, file names, message text, email addresses or member ids in metrics tables,
  and `/stats` shows no per-member breakdown. This keeps the existing "Member data isolation"
  requirement intact with the owner as reader.
- **90-day retention**, pruned during the tick.
- **Telegram calls are counted** as provider `telegram`, successes and failures alike, in the
  Telegram client itself (one place, with timing), although Telegram does not go through `Gateway`.
- **`/stats` covers the past 7 days**, with no argument.

## Risks / Trade-offs

- [A metrics write fails, for example a locked database] → Recording is wrapped so that it logs and
  returns; it never raises into the tick, a webhook job or a provider call.
- [Service names in the run log] → Failed-service names are public terms pages shared by all watchers,
  not member data; they reveal that someone watches the service, which `/members` plus the shared
  design already implies. Acceptable for an owner-only view.
- [Failing the workflow on any failed service is noisy if one site blocks scrapers for days] → Start
  strict; if it becomes noise, change the workflow to fail above a threshold. No app change needed.
- [Provider counts miss page fetches and history sources] → Accepted; those failures already surface
  as failed services in the run log.

- [A provider's cost field is absent, renamed or shaped differently from the docs] → Fields are
  optional; `/stats` shows "not reported" for that provider rather than zero, so a gap is visible.
- [The dollar total understates spend] → `/stats` lists the non-dollar units beside the total and says
  they are not included. Sprite compute is named as excluded.
- [The connector does not forward a needed path or query parameter] → Checked on the Sprite; all
  sources in the cost table were observed through the connectors.

## Migration Plan

- Schema version 4 creates the metrics tables; additive, applied on start-up by the existing migration
  path, with the usual pre-migration backup copy.
- Rollback: deploy the previous code. The extra tables are ignored by older code; `user_version`
  stays at 4, which older code treats as up to date.

## Open Questions

- Whether Sprite process memory survives a pause. Not needed by this design, which keeps no metrics in
  memory.

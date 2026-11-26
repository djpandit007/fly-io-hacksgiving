# Proposal

## Why

Fine Print has log lines and a static `/health`, and nothing else. The owner cannot tell whether the
daily run worked, how the external providers are behaving, or whether members use the bot, without
reading Sprite logs. A paid observability service is ruled out, and the Sprite pauses about 30 seconds
after the last request, so the usual scrape-based metrics stack does not fit.

## What Changes

- Record operational counts in the existing SQLite database:
  - each daily run: services checked, services that failed, changes found, nudges, deliveries sent
    and deliveries that failed
  - background jobs (Telegram messages, forwarded mail) that ended in an error
  - calls to each external provider, by outcome (ok, error, retried), with average and slowest
    response time per day
  - how long each daily run and each background job took
  - what each provider reports having charged or consumed, read from the provider's own responses:
    US dollars where the provider reports money (OpenCode `usage.cost`, Exa `costDollars`), and the
    provider's own unit where it does not (LlamaCloud credits, Pinecone read units and embedding
    tokens); Jev token counts are kept beside its dollar cost
  - bot commands used, by command name
- Add an owner-only `/stats` bot command that reports the past 7 days: recent daily runs and their
  failures, provider call counts, error counts and response times, reported spend in US dollars with
  the non-dollar units listed beside it, and usage totals (members, watched services, material
  changes, commands used).
- Cost is never computed from a price stored in configuration. A figure is shown in dollars only when
  a provider reported it in its response. Everything else
  is shown in the provider's unit and labelled as not included in the dollar total.
- Make the daily GitHub Actions workflow write the run's summary to the job summary page and fail when
  any service failed to fetch, so GitHub notifies the owner. This is also the only signal that the
  daily run never happened, because a sleeping Sprite cannot report its own missed run.
- Keep metrics for 90 days.

Not in this change: alerts pushed by the bot, a JSON or Prometheus endpoint, latency percentiles or
graphs, the Sprite's own compute cost (not visible from inside the app), currency conversion,
OpenTelemetry, and any external metrics store.

## Capabilities

### New Capabilities

- `owner-metrics`: what the system records about its own runs, provider calls, response times,
  provider-reported cost and usage; the rule that cost comes from the provider and never from a
  configured price; the rule that
  metrics hold counts only and never a member's URLs, file names or per-member activity; how long they
  are kept; and how the owner reads them (`/stats`, and the daily run's summary and failure status).

### Modified Capabilities

None. `/stats` is owner-only in the same way as `/members`, but its behaviour belongs to
`owner-metrics`; no `bot-membership` requirement changes, and "Member data isolation" is respected by
the counts-only rule rather than amended.

## Impact

- **Database:** schema version 4 adds metrics tables; migrates on start-up like versions 2 and 3.
- **Code:** `app/db.py` (migration), a new metrics module, `app/gateway.py` and `app/agent.py`
  (`Clients` passes a recorder to each `Gateway`), `app/main.py` (tick and job handlers),
  `app/notify.py` (delivery failures are counted, not only logged), `app/bot.py` (`/stats`, command
  counts, owner help text), and the provider modules that must now read cost and usage fields they
  ignore today: `app/writer.py`, `app/judge.py`, `app/exa.py`, `app/parse.py`, `app/memory.py`.
- **Provider requests:** LlamaCloud job reads add `expand=usage`; nothing else changes on the wire.
- **Workflow:** `.github/workflows/tick.yml`.
- **Docs:** README command table.
- **Dependencies:** none added.

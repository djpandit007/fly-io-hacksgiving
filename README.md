# Fine Print

A personal agent that reads the fine print you agree to every day, so you don't have to.

It lives on a [Fly.io Sprite](https://sprites.dev), watches the terms of the services you use, and
messages you on Telegram only when something **material** changes: a new arbitration clause, your
data used for AI training, a price that now auto-renews. When a change gives you a window to opt
out, the deadline goes into your calendar.

Built for [Fly.io Hacksgiving](https://fly.io/hacksgiving), Nov 23–29, 2026.

```
 daily cron --POST /tick-->+                +<--webhook-- Telegram (you)
                           |                |
                     +-----v----------------v------+
                     |  Sprite: FastAPI on :8080   |
                     |  SQLite + snapshots on disk |
                     +--+------+------+------+-----+
 Gmail (read-only) <----+      |      |      +-----> /calendar.ics
 terms history (OTA/Wayback) <-+      |
 connectors: TypeSafe · Pinecone · Ollama Cloud · Box · LlamaParse (Custom API)
```

## What it does

| Loop | How |
|---|---|
| **Terms watch** (daily) | Fetch each terms page → normalise → section diff against the last snapshot → Jev judges each changed section → material ones get a plain-English summary and a Telegram nudge |
| **History on day one** | New services are backfilled with a year of versions from Open Terms Archive or the Wayback Machine, so real past changes show up immediately |
| **Before you agree** | Send a link, PDF or screenshot → red-flag card (auto-renewal, arbitration, data sharing…) with the ToS;DR community grade |
| **Deadline ledger** | Opt-out windows from changes, plus deadlines LlamaExtract pulls from your own documents (lease, offer letter) → `/calendar.ics` and nudges 3 days ahead |
| **Inbox scan** | Read-only Gmail headers find the services you signed up for and the "we updated our terms" emails |
| **Agreed Wrapped** | Your year of accepted terms, as a dissenting opinion |

## Who does what

| Piece | Role |
|---|---|
| **Sprite** | The agent's home: SQLite, snapshots, a service on :8080; holds a task while it works |
| **TypeSafe (Jev)** | Typed judgments per change: material (Noul), category and who it favours (Choice), opt-out (Noul), severity (Score) |
| **Ollama Cloud** | Plain-language summaries, verdict cards, Wrapped |
| **Pinecone** | Clause memory: "you already accepted similar wording from Spotify and Uber" |
| **Box** | Drop folder for your own documents; archive of change reports |
| **LlamaParse / LlamaExtract** | PDFs, scans, screenshots and JS-heavy pages → per-page Markdown; dated obligations with page citations |
| **ToS;DR** | Community A–E grade beside the agent's own verdict |

Code owns the workflow; models answer narrow questions. Every external response is validated
with Pydantic at the boundary, so an API change fails loudly instead of silently mis-judging.

## Run locally

```bash
uv sync
cp .env.example .env    # fill in direct provider URLs and keys
uv run pytest
uv run uvicorn app.main:app --reload --port 8080
```

## Deploy to a Sprite

1. In the Fly.io dashboard → Sprites → Connectors, add **TypeSafeAI**, **Ollama Cloud**,
   **Pinecone**, **Box**, and a **Custom API** for LlamaCloud (`https://api.cloud.llamaindex.ai`,
   bearer token). Restrict each to the `fineprint` label.
2. `PINECONE_API_KEY=… ./scripts/setup_pinecone.sh` creates the index.
3. `cp .env.example .env.sprite`, fill in secrets and Telegram token, then `./scripts/setup_sprite.sh`.
4. `./scripts/connectors.sh`, copy each gateway base into `.env.sprite`, set `FP_PUBLIC_URL`, redeploy.
5. Register the Telegram webhook (command printed by the setup script), then send `/start <FP_OWNER_CLAIM_CODE>` to your bot.
6. Optional: `uv run python scripts/gmail_auth.py CLIENT_ID CLIENT_SECRET` and push `gmail.json`.
7. Add repo secrets `FINE_PRINT_URL` and `FINE_PRINT_SECRET`; the workflow in
   `.github/workflows/tick.yml` wakes the Sprite daily (Sprites cannot wake themselves on a schedule).

## Bot commands

`/watch <url|domain>` · `/list` · `/scan` · `/approve <name|all>` · `/deadlines` · `/wrapped` ·
or just send a link, PDF or photo.

## Privacy and limits

- Gmail scope is `gmail.readonly`; only `From` and `Subject` headers are read; nothing from mail is stored but sender domains.
- Provider credentials stay in Fly.io connectors, not on the Sprite. The Telegram token and Gmail refresh token have no connector and live in `0600` files on the Sprite.
- One owner: the chat that sends `/start <claim code>` (or `FP_OWNER_CHAT_ID`) owns the bot; all other chats are ignored.
- Links you send and links found on pages or in email are fetched only if every redirect hop resolves to a public address.
- The Telegram token is redacted from errors, and request URLs are not logged.
- **Information, not legal advice.**

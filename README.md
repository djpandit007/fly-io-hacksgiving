# Fine Print

An agent that reads the fine print you agree to every day, so you don't have to — for you and the
people you share it with.

It lives on a [Fly.io Sprite](https://sprites.dev), watches the terms of the services each member uses,
and messages them on Telegram (or by email) only when something **material** changes: a new arbitration
clause, your data used for AI training, a price that now auto-renews. When a change gives you a window to
opt out, the deadline goes into your own calendar.

Built for [Fly.io Hacksgiving](https://fly.io/hacksgiving), Nov 23–29, 2026.

```
 daily cron --POST /tick-->+                 +<--webhook-- Telegram (members)
                           |                 +<--webhook-- AgentMail (forwarded email)
                     +-----v-----------------v-----+
                     |  Sprite: FastAPI on :8080   |
                     |  SQLite + snapshots on disk |
                     +--+------+------+------+-----+
 terms history (OTA/Wayback) <-+      |      +-----> /calendar.ics?token=<member's own>
 connectors: Pinecone · AgentMail · Exa · OpenCode (Custom API) · LlamaParse (Custom API)
```

## What it does

| Loop | How |
|---|---|
| **Terms watch** (daily) | Fetch each watched terms page → normalise → section diff against the last snapshot → Jev judges each changed section → material ones get a plain-English summary, sent to every member who watches that service |
| **History on day one** | A newly watched service is backfilled with a year of versions from Open Terms Archive or the Wayback Machine; the next member to watch it gets that history instantly |
| **Before you agree** | Send a link, PDF or screenshot → red-flag card (auto-renewal, arbitration, data sharing…) with the ToS;DR community grade |
| **Deadline ledger** | Opt-out windows from changes, plus deadlines LlamaExtract pulls from your own documents (lease, offer letter) → your private calendar feed and nudges 3 days ahead |
| **Email intake** | Forward "we updated our terms" emails, receipts and contracts to the shared inbox → verdicts in Telegram and by email |
| **Agreed Wrapped** | Your year of accepted terms, as a dissenting opinion |

**Judged once, shared by everyone:** terms are public and identical for all members, so each service is
fetched, diffed and judged once and the result fans out to every watcher. Cost grows with the number of
distinct services, not members.

## Who does what

| Piece | Role |
|---|---|
| **Sprite** | The agent's home: SQLite, snapshots, a service on :8080; holds a task while it works |
| **OpenCode → Jev** | TypeSafe's decision model, served by OpenCode: typed judgments per change — material (Noul), category and who it favours (Choice), opt-out (Noul), severity (Score) |
| **OpenCode → chat model** | Plain-language summaries, verdict cards, Wrapped (`glm-5.3-flash` by default) |
| **Pinecone** | Embeddings and clause memory: "you already accepted similar wording from Spotify and Uber" — limited to *your* services |
| **AgentMail** | The shared inbox members forward mail to, and email replies |
| **Exa** | Finds a service's terms page from a name (`/watch notion`) and reads pages that block scrapers |
| **LlamaParse / LlamaExtract** | PDFs, scans, screenshots and attachments → per-page Markdown; dated obligations with page citations |
| **ToS;DR** | Community A–E grade beside the agent's own verdict |

Code owns the workflow; models answer narrow questions. Every external response is validated with
Pydantic at the boundary, so an API change fails loudly instead of silently mis-judging.

## Sharing Fine Print

- **Owner:** you. Claim the bot once with `/start <FP_OWNER_CLAIM_CODE>` (or fix it with `FP_OWNER_CHAT_ID`).
- **Members:** anyone you give the join code to sends `/start <FP_JOIN_CODE>` to the bot. Put the code in
  your submission or send it to colleagues.
- **Email-only members:** someone without Telegram emails the shared inbox with the join code in the
  subject, and gets verdicts back by email.
- **Revoking:** `/remove <id>` (see `/members`) stops serving one member. Change `FP_JOIN_CODE` and
  redeploy to stop new joins with the old code.

Each member's watchlist, deadlines, calendar feed and Wrapped are private to them.

## Bot commands

| Command | Who | What |
|---|---|---|
| `/start <code>` | anyone | claim the bot (owner code) or join it (join code) |
| `/watch <url, domain or name>` | members | watch a service's terms; shows its past year of material changes |
| `/list` | members | your watchlist with community grades |
| `/deadlines` | members | your upcoming deadlines and your private calendar link |
| `/wrapped` | members | your year in accepted terms |
| `/email <address>`, `/verify <code>` | members | link your email so forwarded mail reaches you |
| `/members` | owner | who has joined |
| `/remove <id>` | owner | remove a member |
| *a link, PDF, screenshot or photo* | members | "before you agree" check |

## Run locally

```bash
uv sync
cp .env.example .env    # fill in direct provider URLs and keys
uv run pytest
uv run uvicorn app.main:app --reload --port 8080
```

## Deploy to a Sprite

1. In the Fly.io dashboard → Sprites → Connectors, add **Pinecone**, **AgentMail**, **Exa**, and two
   **Custom API** connectors with bearer tokens: OpenCode (`https://opencode.ai/inference`) and LlamaCloud
   (`https://api.cloud.llamaindex.ai`). Restrict each to Sprite name prefix `fly-io-hacksgiving`.
2. Create the Pinecone index once (the connector can't): in the Pinecone console, a dense index named
   `fine-print`, 1024 dimensions, cosine, serverless — or `PINECONE_API_KEY=… ./scripts/setup_pinecone.sh`.
3. `cp .env.example .env.sprite`, fill in secrets, the Telegram token and `FP_JOIN_CODE`, then
   `./scripts/setup_sprite.sh`.
4. `./scripts/connectors.sh`, copy each gateway base into `.env.sprite`, set `FP_PUBLIC_URL`, redeploy.
5. Register the Telegram webhook (command printed by the setup script), then send
   `/start <FP_OWNER_CLAIM_CODE>` to your bot.
6. Set up the shared inbox and its webhook (see *Email intake*).
7. Add repo secrets `FINE_PRINT_URL` and `FINE_PRINT_SECRET`; the workflow in
   `.github/workflows/tick.yml` wakes the Sprite daily (Sprites cannot wake themselves on a schedule).

The database upgrades itself on start-up (`PRAGMA user_version`); a `fineprint.db.v1.bak` copy is kept
when a single-owner database is migrated.

## Privacy and limits

- Provider credentials stay in Fly.io connectors, not on the Sprite. The Telegram token and the AgentMail
  webhook secret have no connector and live in a `0600` env file on the Sprite.
- Documents and links members send are read by the operator's AI providers; Fine Print keeps only
  extracted deadlines, watchlists and verified email addresses — not message bodies or attachments.
- Non-members get no service: a `/start` without a valid code gets one explanation; everything else is ignored.
- Links you send, links found on pages or in email, and search results are fetched only if every redirect
  hop resolves to a public address.
- The Telegram token is redacted from errors, and request URLs are not logged.
- **Information, not legal advice.**

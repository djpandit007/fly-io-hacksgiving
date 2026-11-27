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
| **Ask about your terms** | `/ask can Spotify raise my price?` (or just type the question) → a short answer from the terms you watch, with the clauses quoted word for word and checked against the stored text; says so when your terms don't cover it |

**Judged once, shared by everyone:** terms are public and identical for all members, so each service is
fetched, diffed and judged once and the result fans out to every watcher. Cost grows with the number of
distinct services, not members.

## Who does what

| Piece | Role |
|---|---|
| **Sprite** | The agent's home: SQLite, snapshots, a service on :8080; holds a task while it works |
| **OpenCode → Jev** | TypeSafe's decision model, served by OpenCode: typed judgments per change — material (Noul), category and who it favours (Choice), opt-out (Noul), severity (Score) |
| **OpenCode → researcher, writer, verifier** | `/ask` splits the work: a researcher agent (tools over your watched terms only) finds the clauses and quotes them, code checks every quote against the stored text and Jev ranks them by relevance, a writer answers only from those clauses, and a verifier on a different model (`glm-5.3-flash`, GLM) rejects claims the clauses don't state; after two rejections you get the quotes alone |
| **OpenCode → chat model** | Plain-language summaries, verdict cards, /watch overviews, Wrapped (`deepseek-v4.1-flash` by default: discounted on OpenCode Zen, zero data retention per Zen docs) |
| **Pinecone** | Embeddings and clause memory: "you already accepted similar wording from Spotify and Uber" — limited to *your* services |
| **AgentMail** | The shared inbox members forward mail to, and email replies |
| **Exa** | Finds a service's terms page from a name (`/watch notion`) and reads pages that block scrapers |
| **LlamaParse / LlamaExtract** | PDFs, scans, screenshots and attachments → per-page Markdown; dated obligations with page citations |
| **ToS;DR** | Community A–E grade beside the agent's own verdict |

Code owns the workflow; models answer narrow questions. Every external response is validated with
Pydantic at the boundary, so an API change fails loudly instead of silently mis-judging.

## Sharing Fine Print

- **Owner:** you. Claim the bot once with `/start <FP_OWNER_CLAIM_CODE>` (or fix it with `FP_OWNER_CHAT_ID`).
- **Members:** share the join link `https://t.me/<your_bot>?start=<FP_JOIN_CODE>`: opening it and
  pressing Start joins in one tap. Sending `/start <FP_JOIN_CODE>` to the bot works too. Put the link in
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
| `/ask <question>` | members | ask about the terms you watch; answered with quotes from them. A plain question typed without `/ask` works too. Answers come from each watched service's terms page only (not its privacy policy), and asking about a service you don't watch offers to watch it |
| `/deadlines` | members | your upcoming deadlines and your private calendar link |
| `/wrapped` | members | your year in accepted terms |
| `/jurisdiction <region>` | members | where you live (`US-CA`, `US-NY`, `US-OTHER`, `EU`, `UK`), for local consumer-law notes on checks |
| `/dashboard` | members | a private sign-in link to your web Control Room (single use, expires in 10 minutes) |
| `/email <address>`, `/verify <code>` | members | link your email so forwarded mail reaches you |
| `/members` | owner | who has joined |
| `/remove <id>` | owner | remove a member |
| `/stats` | owner | the past 7 days: daily runs and failures, provider calls and response times, cost the providers reported, usage totals, answer feedback |
| *a link, PDF, screenshot or photo* | members | "before you agree" check |

Typing `/` shows the command menu; the owner's menu also lists the owner commands. Before someone joins,
the bot's description (shown in an empty chat) says it is private and how to get access.

**Buttons.** `/help` and the welcome have buttons for the main actions, including **Control Room**
(same as `/dashboard`). `/watch` on its own asks which
service and takes the reply as the name. A single word such as `notion` gets "Watch Notion?" with Yes/No.
`/list`, change alerts and `/watch` results have unwatch buttons, with Undo. Each red flag on a verdict
card has a 💡 button that explains it. Verdict cards, change alerts, `/watch` overviews, `/wrapped` and
`/ask` answers have 👍/👎, with an optional reason after 👎.

**On the web.** The Sprite's URL (`FP_PUBLIC_URL`) serves a private, per-member **Control Room**, opened
with the link `/dashboard` sends you in Telegram. There are no public pages: `/` goes to the Control Room,
and every page shows only your own data.

- `/room` — your Terms Radar (the services you watch, ranked by their worst material change in the past
  90 days, with Policy Bundle coverage and ToS;DR grades), their recent material changes and
  contradictions between their documents, upcoming deadlines and calendar feed, jurisdiction, a log of
  actions taken for you (metadata only), and live Swarm Activity.
- `/changes/<id>` — one change on a service you watch: side-by-side redline, Jev's scores (material,
  severity, opt-out, who it favours), a benchmark percentile when available, and the captured screenshot
  when there is one. Changes on services you don't watch are not found.

The sign-in lasts 24 hours; `/remove` ends a member's sessions at once.

**While it works**, the bot puts 👀 on your message, shows a "Thinking…" draft with the current step, and
replies to your message with the result; 👀 becomes 👌, or 😢 if it failed.

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
   `/start <FP_OWNER_CLAIM_CODE>` to your bot. Register it again (`POST /admin/telegram-webhook`) after
   upgrading from a version without buttons: the registration is what asks Telegram for button taps.
6. Set up the shared inbox and its webhook (see *Email intake*).
7. Add repo secrets `FINE_PRINT_URL` and `FINE_PRINT_SECRET`; the workflow in
   `.github/workflows/tick.yml` wakes the Sprite daily (Sprites cannot wake themselves on a schedule).
   The job page shows each run's summary, and the job fails (so GitHub emails you) when a service
   could not be checked or the Sprite could not be reached.

The database upgrades itself on start-up (`PRAGMA user_version`); a `fineprint.db.v1.bak` copy is kept
when a single-owner database is migrated.

## Email intake

One shared [AgentMail](https://agentmail.to) inbox serves every member.

1. In the AgentMail console, create the inbox (e.g. `you-hacksgiving@agentmail.to`) and an **inbox-scoped**
   API key; add that key as the **AgentMail** connector in Fly.io (name prefix `fly-io-hacksgiving`).
2. Set `FP_AGENTMAIL_BASE` (the connector's gateway base), `FP_AGENTMAIL_INBOX_ID` and
   `FP_AGENTMAIL_ADDRESS` (both the inbox address) in `.env.sprite`.
3. The connector cannot create webhooks, so in the AgentMail console create a webhook:
   URL `https://<your-sprite>.sprites.app/agentmail`, event **`message.received`** only, inbox: the one
   above. Copy its signing secret (`whsec_…`) into `.env.sprite` as `FP_AGENTMAIL_WEBHOOK_SECRET`.
4. Redeploy (`./scripts/setup_sprite.sh`) and run `./scripts/check_agentmail.sh`.

How members use it:

- **Telegram members:** `/email you@example.com`, then `/verify <code>` from the email. Afterwards, forward
  terms-update notices, receipts and contracts (as attachments) to the inbox; replies arrive in Telegram
  and by email.
- **Email-only members:** email the inbox with the join code in the subject, then reply to the
  confirmation email with its code. Forwarding works the same way, with replies by email.
- Mail from unknown addresses, and mail AgentMail marks as spam or failing sender authentication, is
  ignored without a reply. Codes are rate-limited and repeated wrong codes pause linking for 24 hours.

## Privacy and limits

- Provider credentials stay in Fly.io connectors, not on the Sprite. The Telegram token and the AgentMail
  webhook secret have no connector and live in a `0600` env file on the Sprite.
- Documents and links members send are read by the operator's AI providers; Fine Print keeps only
  extracted deadlines, watchlists and verified email addresses — not message bodies or attachments.
- Questions you ask are read by the operator's AI providers and are not stored.
- Answer feedback (👍/👎) keeps only the kind of answer, the writer model and prompt version, and for
  public terms changes the change id: never the answer text, a URL, or who rated it. Kept 90 days.
- Button data is never trusted: every tap is checked against the tapping member's own data.
- Non-members get no service: a `/start` without a valid code gets one explanation; everything else is ignored.
- Links you send, links found on pages or in email, and search results are fetched only if every redirect
  hop resolves to a public address.
- The Telegram token is redacted from errors, and request URLs are not logged.
- **Information, not legal advice.**

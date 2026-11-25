# Design

## Context

Fine Print is one FastAPI service on a Fly.io Sprite. It uses a single SQLite connection on the Sprite's
disk (`app/db.py`), a Telegram webhook (`app/main.py`, `app/bot.py`), and connectors reached through the
Sprites gateway (OpenCode for Jev and the writer, Pinecone, LlamaCloud). It wakes once a day from a GitHub
Actions cron. Ownership is a single `kv.owner_chat` row; services, deadlines and the calendar token are
global, and all work is serialized by one `work_lock`. See `proposal.md` for motivation and the specs for
required behavior.

Constraints:
- The Sprite pauses about 30s after the last request, so background work must hold a task (`app/sprite.py`).
- The connector gateway forwards only some endpoints per provider, limits requests to 8 MB, and does not
  follow redirects.
- AgentMail's free tier allows 3 inboxes and 100 emails per day.
- Submission is due Sun Nov 29, 11:59 PM PT.

## Goals / Non-Goals

**Goals:**
- Many members on one Sprite and one SQLite database, with costs that grow with distinct services rather
  than members.
- Email as a second channel that is safe against impersonation and webhook forgery.
- Upgrade the live database in place, with a backup.

**Non-Goals:**
- Per-member usage limits or billing (decided: none).
- One inbox or custom domain per member, or members connecting their own mailbox (OAuth).
- An unwatch command or member self-deletion. The owner's `/remove` covers revocation for now.
- Horizontal scaling beyond one Sprite.

## Decisions

### D1. Users keyed by an internal id, with optional chat and email
`users(id, chat_id UNIQUE NULL, email UNIQUE NULL, role, name, calendar_token, joined_at, status, …)`.
Email-only members have no chat; Telegram members may add an email.
- *Alternative:* key users by `chat_id`. Rejected because email-only members have none.

### D2. Shared services, per-user subscriptions; judge once, render per recipient
`services`, `snapshots` and `changes` stay global. `subscriptions(user_id, service_id)` is the watchlist.
`record_version` keeps storing and judging changes globally and returns the material change rows. A new
`report_for(change, user)` builds the user's `ChangeReport`: precedents filtered to that user's services,
and the opt-out deadline written to that user's ledger. Replay to a new subscriber reads past material
`changes` rows, so `changes` gains `favours` and `partial` to rebuild full verdicts without calling Jev.
Pinecone `remember` stays global; `similar` adds `filter: {"service": {"$in": [user's services]}}`.
- *Alternative:* per-user copies of services. Rejected: cost and latency multiply by the number of members.

### D3. Schema v2 migration via `PRAGMA user_version`
`connect()` runs pending migrations in one transaction:
- create `users`, `subscriptions` and `mail_seen`;
- rebuild `ledger` with `user_id` and `UNIQUE(user_id, title, due_on)`, copying rows with
  `user_id NULL`;
- add `changes.favours` and `changes.partial`;
- stop using `services.status` and `services.source`.

Before migrating, the database file is copied to `fineprint.db.v1.bak`. On owner claim or configuration,
"adopt legacy data" runs:
- subscribe the owner to services with no subscribers;
- assign `user_id NULL` ledger rows to the owner;
- adopt the old `kv.owner_chat`, if still present, as the owner.

- *Alternative:* start a fresh database. Rejected: it is cheap to preserve data and the pattern is needed
  for future schema changes.

### D4. Per-key locks instead of the global lock
A small lock registry yields an `asyncio.Lock` per key:
- `chat:<id>`, `email:<addr>`: one message at a time per member;
- `service:<id>`: backfill and checks of one service never run twice at once;
- `tick`: one daily run at a time.

The single SQLite connection is shared. Statements are autocommit, and multi-row writes (migration,
membership changes, adoption) use `db.transaction()`. Task holds remain per job, as today.
- *Alternative:* keep the global lock. Rejected: one member's 60-second backfill would stall everyone.

### D5. Codes and verification
- Owner claim and join codes are compared with `hmac.compare_digest`.
- Email verification codes are 6 digits from `secrets.randbelow`, stored as `sha256(user_id:code)` with a
  15-minute expiry and at most 5 attempts.
- An address becomes `email` only on successful verification; until then it sits in `email_pending`.
- `/email` for an address verified by another member is refused.

### D6. AgentMail: one shared inbox, push via webhook
- Inbound mail arrives at `POST /agentmail`, delivered by Svix.
- Verification: the secret is the base64 part after `whsec_`; the signed content is
  `"{svix-id}.{svix-timestamp}.{raw body}"`; HMAC-SHA256, base64. Any `v1,<sig>` in the space-separated
  `svix-signature` header must match (constant time). The timestamp must be within ±300s.
- Only `event_type == "message.received"` is accepted. The "unauthenticated" and "spam" variants are not
  subscribed.
- The handler dedupes on `message_id` (`mail_seen`), takes a task hold, answers 200, and processes in the
  background.
- *Alternative:* polling. Rejected: the Sprite is asleep between requests and polling would need a frequent
  external cron. The Svix secret is stored on the Sprite like the Telegram token, since connectors cannot
  hold inbound secrets.

### D7. Processing a forwarded email
1. Identify the sender with `parseaddr(from)`, lower-cased.
   - A verified member: continue.
   - The subject contains the join code: create an email-only member with this address verified, then
     continue.
   - Otherwise: stop silently.
2. Fetch the full message (text, html, attachments). Download each attachment via its `download_url` with
   `netguard.safe_client`, which refuses private addresses; skip anything over 8 MB.
3. Classify:
   - **Attachments** (PDF or image) → `intake_file` for each; deadlines go into the member's ledger.
   - **Terms-update notice**: the subject matches the existing update pattern (update/change + terms/policy).
     The original sender domain is the forwarded `From:` line in the body when present, else the sender's
     domain. Discover and subscribe that domain's terms, then check it now.
   - **Link**: the body contains a URL whose path looks like terms, legal, privacy or agreement →
     `check_url`.
   - **Body text**: at least 300 characters → `verdict_card` on the text.
   - **Otherwise**: a short help reply.
4. Reply in Telegram if a chat is linked (HTML card) and by email (`messages/{id}/reply`, plain-text
   rendering of the same card). Bodies and attachments are discarded after processing.

### D8. Exa for discovery and text fallback
- Discovery: `POST /search` with `query = "<name> terms of service"`, `numResults = 5`, and
  `includeDomains = [domain]` when a domain is known. Pick the first result whose URL or title matches a
  terms pattern. The existing home-page link scraper is the fallback.
- Page text: direct fetch through `netguard` first. If it errors, returns non-2xx, or yields fewer than 400
  characters, call `POST /contents` with `{urls: [url], text: true}`. PDFs go to LlamaParse.
- URLs from Exa are still fetched only through `netguard`.
- *Alternative:* Firecrawl. Not chosen: Exa covers search and content in two endpoints and its connector
  allows exactly those.

### D9. Delivery and rendering
- Each tick returns `recipient → [messages]`. A recipient is a member's chat, or their email if they have
  no chat.
- Telegram gets one message per item. Email gets a single digest per member per run, to stay within 100
  emails per day.
- `telegram.rich` renders HTML; a new `plain()` renders text for email.
- Telegram sends to many chats are sequential with a short pause, to respect bot rate limits.

### D10. Calendar per member
`users.calendar_token` is 24 random hex characters, generated at join. `/calendar.ics?token=` resolves the
member and returns their ledger. The legacy `FP_CALENDAR_TOKEN` resolves to the owner.

### D11. Removing Box and Gmail
- Delete `app/box.py`, `app/gmail.py`, `scripts/gmail_auth.py` and their settings, the Box inbox step in
  `tick`, `archive_to_box`, and the `/scan` and `/approve` commands.
- The `kv.box_seen` row is left unused.

## Risks / Trade-offs

- **AgentMail 100 emails/day** (verification codes, replies, digests): one digest per member per run;
  failed sends are logged and Telegram still delivers. Upgrade the plan if usage grows.
- **The connector may block inbox or webhook creation:** create the inbox and webhook once in the AgentMail
  console; the app only needs send, reply and message read, which the connector does allow.
- **Forwarded-mail formats vary**, so the original-sender heuristic can miss: fall back to the sender's
  domain, then to a body-text verdict. The member still gets an answer.
- **A spoofed `From` of a verified member:** only `message.received` (authenticated) is accepted. Worst
  case, the verdict is emailed to the real owner of that address and deadlines are added to their ledger.
  No data leaks to the spoofer.
- **A leaked join code** lets strangers in, and they spend the operator's credits: rotate `FP_JOIN_CODE`
  and `/remove` the affected members. Usage limits are a follow-up if needed.
- **No usage limits:** watch OpenCode and LlamaCloud spend during judging.
- **A migration bug on the live database:** an automatic `.v1.bak` copy is taken first; roll back by
  restoring it and redeploying the previous commit.
- **One shared SQLite connection with interleaved tasks:** keep each logical write atomic
  (`transaction()`), and rely on the per-service and per-member locks for ordering.
- **Fan-out to many chats is slow:** sequential sends are fine at hackathon scale; batching is a follow-up.

## Migration Plan

1. Deploy. On start-up the v2 migration runs (backup first) and the app serves as before.
2. The owner claims again (the old owner record was cleared earlier); legacy data is adopted.
3. Set `FP_JOIN_CODE`, the Exa and AgentMail gateway bases, the inbox id and address, and the webhook
   secret in `.env.sprite`; redeploy.
4. In the AgentMail console (or via the gateway, if allowed), create the inbox and the `message.received`
   webhook pointing at `https://fly-io-hacksgiving-4xqz.sprites.app/agentmail`.
5. Verify live: two Telegram accounts, one email-only member, and the GitHub tick workflow.

**Rollback:** redeploy the previous commit and copy `fineprint.db.v1.bak` back over `fineprint.db`.

## Open Questions

- The inbox's address (`fineprint@agentmail.to` or a variant, if taken). This is cosmetic and decided at
  setup.

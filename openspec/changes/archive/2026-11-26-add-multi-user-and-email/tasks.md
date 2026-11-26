# Tasks

## 1. Remove Box and Gmail

- [x] 1.1 Delete `app/box.py`, `app/gmail.py`, `scripts/gmail_auth.py` and the Box/Gmail settings in `app/config.py` and `.env.example`; remove `archive_to_box`, `process_box_inbox`, the Gmail hints in `tick`, and `/scan`/`/approve` in `app/bot.py`. Verify `git grep -i -E "box|gmail" -- app scripts` finds no live references and `uv run pytest` passes after removing their tests.

## 2. Schema v2 and membership

- [x] 2.1 Add the v2 migration to `app/db.py`, run from `connect()` under `PRAGMA user_version` with a `fineprint.db.v1.bak` backup: `users`, `subscriptions`, `mail_seen`, rebuilt `ledger` with `user_id` and `UNIQUE(user_id, title, due_on)`, and `changes.favours`/`partial`. Add typed `User`/`Subscription` rows and `LedgerEntry.user_id`. Verify with a test that upgrades a v1 database (one service, one ledger row) and is idempotent on a second `connect()`.
- [x] 2.2 Create `app/users.py` (`claim_owner`, `join_member`, `by_chat`, `by_email`, `by_calendar_token`, `members`, `remove`, `adopt_legacy`, per-user `calendar_token`) and add `FP_JOIN_CODE` to config and `.env.example`. Verify with tests for owner claim, configured owner, wrong code, idempotent join, removed member, and adoption of legacy services, ledger rows and `kv.owner_chat`.
- [x] 2.3 Add a per-key lock registry and replace the global `work_lock` in `app/main.py` with per-chat and tick locks. Verify with a test that two chats' handlers run concurrently while one chat's run in order.

## 3. Multi-user terms watch and deadlines

- [x] 3.1 Make the ledger per user (`ledger.add/upcoming/due_for_nudge` take `user_id`) and serve `/calendar.ics` by per-user token, with the legacy `FP_CALENDAR_TOKEN` mapped to the owner. Verify with tests that each feed contains only its member's deadlines, that an unknown token returns 401, and that per-user deduplication works.
- [x] 3.2 In `app/agent.py`, add `subscribe` (create the service if new and backfill it once under a per-service lock, then replay past-year material changes from `changes` without judge calls) and store `favours`/`partial` on changes. Verify with tests that a second subscriber triggers zero Jev calls but gets both reports, and that two concurrent first watches backfill once.
- [x] 3.3 Split rendering into `report_for(change, user)`: per-user Pinecone precedents (`memory.similar` with a `service $in` filter) and the opt-out deadline in that user's ledger. Verify with tests that the Pinecone request body carries only the user's services and that deadlines land in each subscriber's ledger.
- [x] 3.4 Rework `tick` to check services with at least one active subscriber and return `recipient → messages` (per-user change reports and nudges). Update `/tick` in `app/main.py` to deliver to each chat. Verify with a test where one judged change reaches two subscribers' chats and none reaches a non-subscriber, and the removed-last-watcher service is skipped.
- [x] 3.5 Make `wrapped`, `intake_file` and `verdict_card` user-aware, and update `app/bot.py` for roles: `/start` claim/join/explain, owner-only `/members` and `/remove`, per-user `/list`, `/deadlines` (with feed URL), `/wrapped` and `/watch`, and the member welcome text; parse `from` names in `app/telegram.py`. Verify with tests for isolation (A cannot see B's list, deadlines or Wrapped), owner-only commands, and the stranger-silence rules.
- [x] 3.6 Update the README (sharing the bot, commands by role, privacy) and verify the documented commands match `bot.HELP`.

## 4. Exa terms discovery and page text

- [x] 4.1 Probe the Exa connector from the Sprite (`/search` and `/contents` through the gateway) and add `FP_EXA_BASE` to config and `.env.example`. Verify one live search returns a terms URL for "notion".
- [x] 4.2 Use Exa search in `discover.find_terms_url` (name or domain, `includeDomains` when known, terms-pattern pick, scraper fallback) and Exa `/contents` in `agent.page_text` when a direct fetch fails or is under 400 characters. Verify with respx tests for name-only discovery, the fallback to scraping, a blocked page served by `/contents`, and that netguard still refuses a private URL returned by search.

## 5. AgentMail email intake

- [x] 5.1 Probe the AgentMail connector from the Sprite (send, reply, get message, attachment, and whether inbox or webhook creation is allowed). Create the inbox, by API or console, and add `FP_AGENTMAIL_BASE`, `_INBOX_ID`, `_ADDRESS` and `_WEBHOOK_SECRET` to config and `.env.example`. Verify a test email can be sent from the inbox to the owner.
- [x] 5.2 Create `app/mail.py` with send, reply, get message and attachment download (through `netguard`), and `/email` and `/verify` in the bot (6-digit hashed code, 15-minute expiry, 5 attempts, refusal of an address verified by someone else). Verify with tests for the happy path, wrong code, expired code, too many attempts, and an address in use.
- [x] 5.3 Add `POST /agentmail` with Svix signature verification (±300s), `message.received` only, `mail_seen` dedupe, task hold, and background processing. Verify with tests for valid, invalid and stale signatures, other event types, and duplicate delivery.
- [x] 5.4 Implement email processing per design D7: sender resolution (verified member, join-code subject creates an email-only member, else silent), then attachments → `intake_file`, terms-update notice → subscribe and check, terms link → `check_url`, long body → verdict, else help. Replies go to Telegram and by email, with one email digest per member per tick. Verify with tests for each branch, unknown-sender silence, and that no body or attachment is persisted.
- [x] 5.5 Write `scripts/setup_agentmail.sh` (or README console steps if the connector blocks creation) for the inbox and webhook, plus a README section on email intake. Verify by following it once for the live inbox.

## 6. Deploy and live verification

- [x] 6.1 Deploy to the Sprite and confirm the v2 migration ran (backup present, `PRAGMA user_version` = 2) and `/health` is OK. The owner re-claims and sees the previously watched service.
- [x] 6.2 Live multi-user check: a second Telegram account joins with the join code; both `/watch youtube.com` (the second gets instant replay); `/watch notion` resolves via Exa; each calendar URL shows only its own deadlines; `/members` lists both.
- [x] 6.3 Live email check: link and verify the owner's address; forward a real "we updated our terms" email and a PDF and receive verdicts in Telegram and by email; an email-only member joins via a join-code subject; mail from an unknown sender gets no reply.
- [x] 6.4 Run the GitHub tick workflow and confirm each member receives only their own messages and the logs show no errors. Then commit and push.

## Workflow follow-up

- Archive the change (`/opsx:archive`) once it is verified live, so its specs become the project's main specs.

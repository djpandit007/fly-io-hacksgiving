# Proposal

## Why

Fine Print is a single-owner Telegram bot: one chat claims it and every other chat is ignored, so colleagues
and the Fly.io Hacksgiving judges (submission due Sun Nov 29, 11:59 PM PT) cannot use it. Two of its
connectors, Box and the Gmail scan, are tied to the owner's own accounts and add nothing for anyone else.
Making the bot multi-user, and choosing connectors that serve every member, makes it shareable and a
stronger entry.

## What Changes

- Members can join with a shared, rotatable join code (`/start <code>`); the owner is still claimed with
  the separate owner claim code. The owner can list and remove members.
- Each member has their own watchlist, deadlines, calendar feed and Wrapped. Members never see each
  other's data.
- A service's terms are fetched, diffed and judged once, and material changes are fanned out to every
  subscriber. A new subscriber to an already-watched service gets its past year of material changes
  immediately, without re-judging.
- "You already accepted similar wording from…" only names services that the asking member watches.
- New email intake through one shared AgentMail inbox. Members link and verify their email address. Mail
  they forward (terms-update notices, receipts, contracts with attachments) gets a verdict in Telegram and
  by email. A sender who puts the join code in the subject becomes an email-only member.
- New Exa-backed terms discovery (`/watch notion` finds the terms page) and page-text fallback for sites
  that block scrapers or render with JavaScript.
- **BREAKING** (owner): removes the Box archive and inbox, the Gmail scan, and the `/scan` and `/approve`
  commands. Email intake replaces the inbox scan for everyone.
- **BREAKING** (operations): the database moves to schema v2 with an automatic migration. The existing
  owner record is adopted on the next owner claim. Calendar feeds become per user; the old single
  calendar token keeps working for the owner.

## Capabilities

### New Capabilities
- `bot-membership`: who may use the bot: owner claim, join code, email-only members, member listing and
  removal, and isolation of each member's data.
- `terms-watch`: per-member watchlists over shared services; judging each change once; fan-out of
  material changes to subscribers; replay of past changes to new subscribers; per-member precedents.
- `deadline-ledger`: per-member dated obligations from terms changes and documents, nudges, and a private
  per-member calendar feed.
- `email-intake`: the shared inbox: verified address linking, email-only joining, processing of forwarded
  mail and attachments, replies, and webhook authenticity.
- `terms-discovery`: finding a service's terms page from a name or domain, and obtaining clean page text
  when a direct fetch is blocked or empty.

### Modified Capabilities
- None (no specs exist yet).

## Impact

- **Code:** `app/db.py` (schema v2 and migration), new `app/users.py` and `app/mail.py`, `app/agent.py`
  (subscriptions, fan-out, replay, per-user reports), `app/memory.py` (metadata filter), `app/ledger.py`
  (`user_id`), `app/discover.py` (Exa), `app/bot.py` (commands and roles), `app/main.py` (locks,
  `/agentmail` webhook, per-user calendar), `app/config.py`. Removed: `app/box.py`, `app/gmail.py`,
  `scripts/gmail_auth.py`.
- **Connectors:** add AgentMail and Exa (name prefix `fly-io-hacksgiving`); the Box connector is no longer
  used. Pinecone, OpenCode and LlamaCloud are unchanged.
- **Configuration:** new `FP_JOIN_CODE`, `FP_AGENTMAIL_*`, `FP_EXA_BASE`; removed `FP_BOX_*` and
  `FP_GMAIL_TOKEN_FILE`.
- **External setup:** an AgentMail inbox and a `message.received` webhook pointing at
  `https://<sprite>.sprites.app/agentmail`.
- **Data:** the live SQLite database is migrated in place; the current test data (one watched service, no
  deadlines) is adopted by the owner.
- **Privacy:** members' documents are processed by the operator's AI providers. Only extracted deadlines
  and verified email addresses are stored.

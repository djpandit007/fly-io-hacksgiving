# Design

## Context

See proposal.md for motivation; the specs in this change define the behaviour.

Current state that shapes the approach:

- **Bot API usage.** `app/telegram.py` uses only `sendMessage` (HTML, previews off), `getFile` and
  `setWebhook`. `allowed_updates` is `["message"]`. `parse_update` returns `None` for anything that isn't a
  message.
- **Request flow.** `app/main.py` answers the webhook at once. It handles the update in a `BackgroundTask`
  under a Sprite hold and a per-chat lock (`chat:<id>`). Long work therefore never blocks Telegram, but a
  second update from the same chat waits for the first.
- **Message handling.** `app/bot.py:handle` parses the text into a command and an argument. It sends
  replies with `tg.send(chat, text)`, which splits text at 4000 characters, and counts the command in
  metrics.
- **Long-running work.** `agent.check_url`, `agent.intake_file`, `agent.subscribe` and `agent.wrapped`
  are single awaits that return HTML. The checked document's text is not kept.
- **Change alerts.** `agent.tick` builds `deliveries: dict[user_id, list[str]]`, and `notify.deliver`
  sends them by Telegram or as an email digest. `intake.py` sends verdicts for forwarded mail to Telegram
  with `tg.send`.
- **Test fake.** Tests use `FakeTelegram` with `send(chat, text)` and `configured` (`tests/test_bot.py`).
- **Database.** SQLite is at schema version 4, with migrations in `app/db.py:MIGRATIONS`.

## Goals / Non-Goals

**Goals:**
- One reusable layer for buttons, taps and progress, shared by typed commands, taps, alerts and forwarded
  mail.
- Stateless where possible: no server-side "pending prompt" or session state.
- Keep every privacy promise: no document text, no URLs, and no member ids in feedback.

**Non-Goals:**
- Streaming the writer's text into the draft. Drafts carry stage text only, and the writer stays
  non-streaming.
- Inline mode, Mini Apps, payments, pinned messages and message effects.
- Rating or buttons in email replies.
- An LLM-based intent router for free text.

## Decisions

### 1. Callback data: a short versioned grammar, never trusted

Every button's data has the form `v1:<action>[:<arg>...]`, which stays under Telegram's 64-byte limit.

| Data | Action | Check at tap time |
|---|---|---|
| `v1:h:list` / `h:dl` / `h:watch` / `h:how` | Help buttons | Active member |
| `v1:w:<name>` | Yes on "Watch X?" | Active member; name re-validated by the suggestion pattern |
| `v1:n` | No on "Watch X?" | None; removes the buttons |
| `v1:u:<service_id>` | Unwatch | The member watches that service |
| `v1:r:<service_id>` | Undo unwatch | The service exists |
| `v1:fx:<flag_key>` | Explain a flag | `flag_key` is in `judge.RED_FLAGS` |
| `v1:fb:<token>:u` / `:d` | Vote | The token exists |
| `v1:fr:<token>:w` / `:c` / `:n` | 👎 reason | The token exists |

- **Parsing.** A small parser returns a typed action or `None`. Unknown data is answered with a short notice
  (bot-membership spec).
- **Why the version prefix.** It lets a later change alter the grammar while old buttons in chats still
  fail safely.
- **Alternative considered: a server-side table of button ids.** It works for any payload size, but it adds
  state and expiry handling. Every payload we need fits in 64 bytes.

### 2. Taps share `Incoming`; fast taps skip the chat lock

`parse_update` maps `callback_query` to `Incoming` with these new fields:

- `kind="tap"`;
- `callback_id`, `data`, and `message_id` (the message the button sits on).

For typed messages it also fills `message_id` and `reply_to_text`, the text of the bot message being
replied to (used in decision 6).

Membership is checked with the chat id of the message the button is on. In a private chat that chat is the
member's.

**Fast taps.** Unwatch, undo, explain, vote, reason, No and all four help actions are single SQL
statements or fixed text. `main._handle_message` runs them without taking `chat:<id>`. That way a tap is
answered at once even while a 60-second check holds the lock.

**Slow taps.** Only Yes on "Watch X?" starts provider work. It routes through the same code as `/watch`,
answers the callback first with "On it…", and then takes the lock.

**Answering every tap.** Every tap is answered with `answerCallbackQuery` exactly once, in a `finally`, so the
button's spinner always stops.

### 3. Sending gains buttons, replies and returned ids

`Telegram.send` gains these parameters:

```
send(chat_id, text, *, buttons=None, reply_to=None) -> list[int]
```

It returns the ids of the messages it sent.

- `buttons` is a list of rows of `(label, data)` pairs. It is sent as `inline_keyboard` on the **last**
  chunk only.
- `reply_to` becomes `reply_parameters={"message_id": ..., "allow_sending_without_reply": True}` on the
  first chunk.

New thin methods:

| Method | Purpose |
|---|---|
| `answer_callback(id, text="", alert=False)` | Stop the spinner; toast or pop-up |
| `edit_buttons(chat, message_id, buttons)` | Change or remove a message's buttons |
| `react(chat, message_id, emoji)` | Set the 👀 / 👌 / 😢 reaction |
| `draft(chat, draft_id, text)` | `sendMessageDraft` |
| `typing(chat)` | `sendChatAction` |
| `set_commands(commands, scope)` | Command menu |
| `set_description(text, short)` | Bot description and short description |

All of them go through `call`, so provider metrics count them like other Telegram calls.

To carry buttons through alerts, a small model `Outgoing(text, buttons=None, feedback=None)` replaces the
bare strings in `TickResult.deliveries`. It is also what `notify.deliver` sends:

- the email path uses only `.text`;
- the Telegram path passes `.buttons` and issues feedback (decision 9).

`notify.Sender` and `FakeTelegram` gain the same optional keyword arguments.

### 4. Progress is a context manager with a stage callback

A new module, `app/progress.py`, provides the context manager:

```
async with progress.working(tg, chat, message_id) as p:
    p.stage("Reading the page…")
    ...
```

**On entry it:**
- sets the 👀 reaction;
- sends an empty draft, which shows Telegram's "Thinking…";
- starts a refresher task that re-sends the current stage every 20 seconds. Drafts last about 30
  seconds, so this keeps the draft alive. After 20 seconds the stage text gains the elapsed time
  ("Still reading… 40s").

**`p.stage(text)`** updates the draft immediately. It reuses the same random non-zero `draft_id`, so
Telegram animates the change.

**If a draft call errors**, the refresher switches to `typing()` every 4 seconds for the rest of the work.

**On exit it:**
- cancels the refresher;
- sets 👌, or 😢 if an exception escaped.

The result itself is sent by the caller with `reply_to=message_id`. Sending it removes the draft.

**Failures never matter.** Every Telegram call inside `working` is wrapped so that its failure is logged
and ignored (reply-progress spec).

**Stage reporting.** `agent.check_url`, `agent.intake_file` and `agent.subscribe` gain an optional
`on_stage: Callable[[str], None]` that defaults to a no-op. They call it at natural boundaries:

- check_url: fetching → reading → judging → writing;
- intake_file: parsing → finding deadlines → judging → writing;
- subscribe: finding history → judging past changes → writing the overview.

For a tap that starts slow work, the reaction goes on the message carrying the button.

**This replaces** the "Reading it…" and "Checking…" messages.

**Alternative considered: one status message edited through the stages, then deleted.** It works for every
client, but it leaves a flash of a message and costs an extra delete. Drafts are available to all bots
since Bot API 9.5, and the typing fallback covers the failure case.

### 5. Bot setup on startup

During startup in `main.lifespan`, best-effort, the bot calls:

- `set_description` and `set_short_description`;
- `set_commands` for `BotCommandScopeAllPrivateChats` (member commands);
- `set_commands` for `BotCommandScopeChat(owner chat)` (member and owner commands), when an owner exists.

`bot._start` repeats the owner-scope call right after a successful `claim_owner`.

The command list is built from the same table as `COMMANDS`, so the menu and the counted commands can't
drift apart.

`set_webhook` sends `allowed_updates=["message", "callback_query"]`.

### 6. ForceReply without state

The "Which service?" prompt is sent with:

```
reply_markup={"force_reply": true, "input_field_placeholder": "e.g. notion or youtube.com"}
```

Its text is a fixed constant, `WATCH_PROMPT`.

When a member's message has `reply_to_text == WATCH_PROMPT`, `handle` treats it as `/watch <text>`. No
pending state is stored.

A forged `reply_to_text` can only make a member's own message act as `/watch`, which they could type
anyway.

### 7. Suggestions for free text

The pattern is `^[A-Za-z0-9][A-Za-z0-9.-]{1,58}$`: one token, with no spaces and no URL, since URLs are
handled earlier. A match gets "Watch <Name>?" with `[Yes]` (`v1:w:<token>`) and `[No]`.

Everything else gets:

```
"Not sure what you mean — try one of these, or send me a link or a document."
```

with the help buttons.

The 58-character limit keeps `v1:w:` plus the name within 64 bytes. The pattern is ASCII-only, so
characters and bytes match.

### 8. Unwatch and undo

`agent.unsubscribe(conn, user, service_id) -> Service | None` deletes the subscription row. It returns the
service, or `None` when the member wasn't watching it. That `None` gives both the "already stopped"
reply and the forged-id case.

`agent.resubscribe(conn, user, service_id) -> Service | None` does `INSERT OR IGNORE` when the service
exists. It makes no provider calls and replays nothing.

**After an unwatch:**
- **On a `/list` message**, the list is rebuilt for the member and the message's buttons are edited in
  place. The toast says "Stopped watching Notion".
- **On an alert or `/watch` result**, the Unwatch button is swapped for `[↩ Undo]` (`v1:r:<id>`) by
  editing the buttons.

`watched_services` already drops services nobody watches, so the daily run needs no change.

### 9. Feedback: a token per rated answer, no member link

Migration v5 adds this table:

```
CREATE TABLE answers (
    token          TEXT PRIMARY KEY,   -- secrets.token_urlsafe(9): 12 chars, unguessable
    kind           TEXT NOT NULL,      -- verdict | alert | overview | wrapped
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    change_id      INTEGER,            -- public terms change, for alert/overview
    quotes_ok      INTEGER,            -- verdict: 1 all passed, 0 some dropped, NULL none asked
    sent_at        TEXT NOT NULL,
    vote           TEXT,               -- up | down
    reason         TEXT,               -- wrong | unclear | not_useful
    voted_at       TEXT
);
```

**Why there is no member id.** In a private chat only the recipient can press the buttons, so ownership is
already enforced by where the message is. An unguessable token stops forged taps from voting on other
answers. That keeps the owner-metrics rule against storing which member did what.

**`app/feedback.py`** provides:
- `issue(conn, meta) -> token | None` (safe: logs and returns `None` on failure, and the answer is then
  sent without rating buttons);
- `vote`, `reason`, `summary(conn, days)` and `prune(conn, days=90)`.

`metrics.report` adds the feedback section, and the daily run calls `prune`.

**Prompt versions** are a constant mapping in `writer.py`:

```
PROMPT_VERSIONS = {"verdict": "v2", "overview": "v1", "alert": "v1", "wrapped": "v1"}
```

`verdict` starts at `v2` because this change edits its prompt. A test checks that changing a prompt's text
changes its version. It does this with a stored hash per prompt, so a silent prompt edit fails CI.

**Rating buttons sit in their own row** under any action buttons.

**After a vote**, the row is edited:
- after 👍 it becomes `[✓ Thanks]`;
- after 👎 it becomes `[Wrong] [Unclear] [Not useful]`;
- after a reason it becomes `[✓ Thanks]`.

The `✓ Thanks` button keeps the voting data, so tapping it again records the same vote without harm.

### 10. Flag explanations and verified quotes

**Explanations.** `judge.FLAG_EXPLANATIONS` sits next to `RED_FLAGS` and has the same keys. A test checks
that both have the same keys and that every explanation is at most 200 characters.

**The explain tap** answers with `answerCallbackQuery(text, show_alert=True)`. Nothing is sent to the chat,
so there's no clutter.

**Card buttons.** The card's explain buttons are `💡 <flag name>`, one per flag found or unclear, two per row.

**Writer prompt.** `writer.verdict_card` asks the writer to end with lines of the form
`QUOTE <flag_key>: <verbatim clause>` for each found flag. `agent.verdict_card` splits those lines off
the card text.

**The check** is a new pure function, `quotes.verify(quote, document) -> bool`. It applies one normaliser to
both texts:

1. `unicodedata.normalize("NFKC")`;
2. map curly quotes and dashes (`“ ” ‘ ’ – — −`) to plain forms;
3. remove the Markdown characters `* _ \` # > |`;
4. collapse whitespace;
5. `casefold()`.

It then tests whether the quote is a substring. Normalised quotes shorter than 25 characters or 4 words
fail.

**Full text.** It compares against the **full** document text, which `verdict_card` already has, not the
12k-character excerpt the writer saw.

**Display.** Quotes that pass are shown as:

```
<blockquote expandable><b>arbitration</b>: …</blockquote>
```

The "What I checked for" list also moves into an expandable quote, so the headline and verdict lead the
card.

**Telegram HTML** supports `<blockquote expandable>`. `plain()` already strips tags for email.

**Alternative considered: a JSON reply from the writer.** It is more robust to parse, but it changes every
caller of `writer.chat` and makes reasoning models more fragile. Line-prefixed quotes degrade gracefully:
if they are unparsed, they stay in the card. To avoid even that, any line starting `QUOTE ` is stripped
whether or not it parses.

## Risks / Trade-offs

- **The 4000-character chunking could cut a `<blockquote>` in half.**
  → Build cards as parts and use `agent.pack` (which never splits a part), as `/watch` already does. Keep the
  quotes block as its own part.
- **A tap arrives while a long check holds the chat lock.**
  → Fast taps skip the lock (decision 2). Slow taps answer the callback before waiting.
- **Drafts could behave differently on old Telegram clients.**
  → The draft is cosmetic and the final message is what matters. Draft errors switch to typing.
- **Writer models may ignore the QUOTE format.**
  → Cards are still sent, and `quotes_ok` in feedback shows how often quotes are dropped, per prompt version.
- **Normalising can make a near-miss quote match**, for example one that differs only in case or
  punctuation.
  → Acceptable: the quoted wording still appears in the document. The minimum length stops trivial
  matches.
- **Answers that are never rated still take a row.**
  → Rows are small and pruned after 90 days.
- **Making `Outgoing` the delivery type touches tick, notify, intake and their tests.**
  → Do it in one task, early, with the tests updated together.
- **Telegram rate limits with many edits and drafts per chat.**
  → Drafts refresh every 20 seconds at most, stage changes are a handful per job, and per-chat limits are about 1
  message per second.

## Migration Plan

1. Deploy. On startup the database migrates to v5 and the bot sets its description and command menus.
2. Run `POST /admin/telegram-webhook` once, so `allowed_updates` includes `callback_query`. Until then,
   buttons show but taps are not delivered.
3. Rollback: the previous build ignores the `answers` table. With the old webhook registration it receives
   no taps. If the webhook was already re-registered, the old `parse_update` returns `None` for taps, so they
   do nothing. Buttons on already-sent messages stay visible but inert.

## Open Questions

- Wording of the bot description and the six flag explanations. It can be tuned during implementation and
  review without changing behaviour.

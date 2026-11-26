# Proposal

## Why

Members who can't work out what the bot does get no guidance: strangers who type anything but `/start` are
ignored, there is no command menu, free text gets the whole help text again, and a link or document check
goes silent for 10–60 seconds behind a "Checking…" line. We also have no signal on whether the answers the
bot writes are useful. Telegram's free Bot API features (command menus, inline buttons, drafts, reactions,
expandable quotes) address all of this without new providers.

## What Changes

- **Guidance before and after joining**: the bot sets its description and short description (what
  strangers see in an empty chat, including how to get access), publishes a `/` command menu (owner
  commands only in the owner's chat), and shares a deep link that joins with one tap.
- **Help with buttons**: `/help` and `/start` show buttons for the main actions. "Watch a service" asks
  "Which service?" with a reply prompt, and the member's reply is treated as `/watch <reply>`. A bare
  `/watch` does the same.
- **Suggestions instead of a help dump**: free text that looks like a service name gets "Watch <name>?"
  with Yes/No buttons; other free text gets a short pointer to help.
- **Progress while working**: a 👀 reaction on the member's message, a "Thinking…" draft that shows the
  current stage, the result as a new message replying to theirs, then 👌 or 😢 on their message.
- **Inline buttons**: the bot now receives button taps. Every tap is checked against the member's own data.
  Old buttons keep working safely.
- **Unwatching**: `/list` shows an unwatch button per service; change alerts and `/watch` results carry
  `[Unwatch]`. Unwatching offers `[Undo]`, which restores the subscription with no new AI work.
- **Explaining flags**: each red flag on a verdict card gets a "What does this mean?" button with a fixed
  plain-English explanation. The card quotes the clause behind each raised flag in an expandable quote.
  The quote is shown only when it matches the document text after normalising both.
- **Answer feedback**: verdict cards, change alerts, `/watch` overviews and `/wrapped` carry 👍/👎, with an
  optional reason after 👎. Only metadata is stored: kind of answer, writer model, prompt version, and the
  public change id. No answer text, URL or member is stored. `/stats` gains a feedback section.
- **Formatting**: verdict detail sits in expandable quotes, and replies thread to the message they answer.

## Capabilities

### New Capabilities
- `bot-guidance`: what the bot tells people so they know what to do: description, command menu, join
  link, help buttons, reply prompts and suggestions for free text.
- `reply-progress`: how the bot shows it is working on a message and how the final reply is delivered.
- `flag-explanations`: plain-English explanations of red flags and verified clause quotes on verdict cards.
- `answer-feedback`: members rating the bot's written answers, what is stored, and how the owner sees it.

### Modified Capabilities
- `terms-watch`: members can stop watching a service and undo it.
- `bot-membership`: button taps follow the same membership and data-isolation rules as messages.
- `owner-metrics`: button taps are counted, and `/stats` includes answer feedback.

## Impact

- **Code**:
  - `app/telegram.py`: new methods: markup on send, callback answers, message edits, reactions, drafts,
    chat actions, bot commands and description; callback-query parsing.
  - `app/bot.py`: tap routing, help buttons, reply prompts, suggestions, progress wrapper.
  - `app/agent.py`: unsubscribe and resubscribe, card markup, quote check.
  - `app/writer.py`: quote output and prompt versions.
  - `app/judge.py`: flag explanations.
  - `app/notify.py`: optional buttons on deliveries.
  - `app/intake.py`: buttons on verdicts delivered to Telegram.
  - `app/metrics.py`: feedback section in `/stats`.
  - `app/db.py`: migration v5 adds the feedback table.
  - `app/main.py`: callback updates, and bot setup on startup.
- **Telegram webhook**: `allowed_updates` gains `callback_query`, so the webhook must be registered again
  (`POST /admin/telegram-webhook`) after deploy.
- **Database**: schema version 5. No change to existing tables.
- **No new providers or dependencies.** The writer prompt for verdict cards changes, so its prompt version
  moves.

# Tasks

## 1. Telegram transport

- [x] 1.1 Extend `Telegram.send` with `buttons` (inline keyboard on the last chunk only) and `reply_to` (`reply_parameters` on the first chunk), returning the sent message ids. Verify with `tests/test_telegram.py` cases using a respx mock that check the request bodies for a one-chunk and a two-chunk message.
- [x] 1.2 Add `answer_callback`, `edit_buttons`, `react`, `draft`, `typing`, `set_commands` and `set_description`, all through `call`. Verify each request's method and payload with respx-mocked tests.
- [x] 1.3 Extend `Incoming` with `kind`, `message_id`, `callback_id`, `data` and `reply_to_text`, and map `callback_query` updates in `parse_update`. Verify by replacing `test_non_message_update_is_ignored` with tests for a tap, a reply to a bot message, and a malformed callback_query (still `None`).
- [x] 1.4 Register the webhook with `allowed_updates=["message", "callback_query"]`. Verify by asserting the `setWebhook` payload in a test.

## 2. Deliveries carry buttons

- [x] 2.1 Add the `Outgoing(text, buttons, feedback)` model. Switch `TickResult.deliveries`, `notify.deliver` and `intake.py`'s Telegram sends to it, with the email path using `.text` only. Extend `FakeTelegram` in the tests to record buttons and `reply_to`. Verify that `pytest tests/test_agent.py tests/test_mail.py tests/test_bot.py` passes with alerts and email digests unchanged in content.

## 3. Progress while working

- [x] 3.1 Create `app/progress.py` with `working(tg, chat, message_id)`. It sets the 👀 reaction, sends the empty draft, refreshes the stage every 20s with elapsed time, falls back to `typing` after a draft error, sets 👌 or 😢 on exit, and swallows every Telegram error. Verify with tests using a fake clock or a short interval, covering: success, failure (😢 and the exception re-raised), draft error falling back to typing, and the reaction call raising without affecting the result.
- [x] 3.2 Add the optional `on_stage` callback to `agent.check_url`, `agent.intake_file` and `agent.subscribe`, called at the stage boundaries in design decision 4. Verify with a test that records the stages for a mocked `check_url`.
- [x] 3.3 Wrap link checks, file checks, `/watch` and `/wrapped` in `bot.handle` with `working`, and remove the "Reading it…" and "Checking…" messages. Send results with `reply_to` the member's message. Verify that bot tests assert no placeholder message is sent, the result replies to the incoming `message_id`, and the reactions sequence is 👀 then 👌.

## 4. Button taps

- [x] 4.1 Add the callback data grammar parser (design decision 1), returning typed actions or `None`, with a 64-byte guard on everything the bot builds. Verify with unit tests for every action, unknown data, wrong version, oversized data and a non-numeric service id.
- [x] 4.2 Route taps in `bot.handle`. Serve taps only from active members and answer non-members with the private-bot notice. Answer unknown data with a short notice. Answer every tap exactly once, in `finally`. Count each tap as `tap:<action>` except votes. Verify with bot tests: stranger tap, removed member tapping an old button, unknown data, and the tap metric being recorded without any service or member.
- [x] 4.3 In `main._handle_message`, run fast taps without the `chat:<id>` lock and take the lock only for slow taps. Verify with a `tests/test_main.py` test where a fast tap completes while the chat lock is held.

## 5. Guidance

- [x] 5.1 Build the member and owner command lists from one table shared with `COMMANDS`. On startup, set the description, short description and both command scopes, best-effort. Repeat the owner scope after a successful claim. Verify with tests that startup and `/start <owner code>` issue the expected `setMyCommands` scopes and lists, and that a Telegram error does not stop startup.
- [x] 5.2 Add help buttons to `/help`, `/start` and the welcome, and handle the `h:*` taps (`list`, `dl`, `how`, `watch`). Verify with bot tests that each tap produces the same reply as its command, and that "How checks work" explains link, PDF, screenshot and photo checks.
- [x] 5.3 Send `WATCH_PROMPT` with ForceReply and a placeholder for bare `/watch` and the `h:watch` tap. Treat a reply to the prompt as `/watch <text>`. Verify with bot tests: a reply watches the service, and the same text sent without the reply is treated as free text.
- [x] 5.4 Replace the free-text help dump: text matching the suggestion pattern gets "Watch <Name>?" with Yes and No buttons, and other text gets the short pointer with the help buttons. Yes runs `/watch` and removes the buttons, and No removes them. Verify with bot tests for a name, a sentence, a 59+ character token, Yes, No, and a stranger sending a name (no reply).
- [x] 5.5 Update `README.md`: the join-link (`t.me/<bot>?start=<code>`) way of sharing, help buttons, unwatching and feedback in the commands section, and the webhook re-registration step. Verify by reading the README sections against the specs.

## 6. Unwatch and undo

- [x] 6.1 Add `agent.unsubscribe` and `agent.resubscribe` (design decision 8). Verify with `tests/test_agent.py` tests: unwatch removes only that member's row; unwatching again returns `None`; a forged id for another member's service changes nothing; resubscribe makes no provider calls; a service whose last watcher unwatched is absent from `watched_services` while its snapshots remain.
- [x] 6.2 Put per-service unwatch buttons on `/list`, and `[Unwatch]` on change alerts and the last `/watch` message. Handle `u:` taps: rebuild the `/list` buttons in place, and on alerts and `/watch` swap the button for `[↩ Undo]`. Handle `r:` taps by resubscribing. Verify with bot tests covering each surface, the toast texts, and undo restoring the watchlist with no AI client used.

## 7. Flag explanations and verified quotes

- [x] 7.1 Add `judge.FLAG_EXPLANATIONS`. Verify with a test that its keys equal `RED_FLAGS` and that each explanation is at most 200 characters.
- [x] 7.2 Add `quotes.verify(quote, document)` with the normaliser in design decision 10. Verify with tests for line breaks and bold in parsed Markdown, table pipes, curly quotes and dashes, case, a quote beyond the 12k excerpt, an invented quote, and a too-short quote.
- [x] 7.3 Ask the writer for `QUOTE <flag>: …` lines in `writer.verdict_card`. In `agent.verdict_card`, strip every `QUOTE ` line, verify each quote against the full text, and render the ones that pass in an expandable quote. Move "What I checked for" into an expandable quote, and build the card as parts packed with `agent.pack`. Verify with tests: a verified quote appears, an invented one doesn't, `QUOTE` lines never leak into the card, and no chunk splits a `<blockquote>`.
- [x] 7.4 Add `💡 <flag>` explain buttons for flags found or unclear, and answer `fx:` taps with a show-alert explanation. Verify with bot tests that the tap answers with the explanation, sends no chat message, and calls no provider.

## 8. Answer feedback

- [x] 8.1 Add migration v5 with the `answers` table. Verify with a `tests/test_db.py` test that upgrades a v4 database, keeps its data, and reaches `user_version` 5.
- [x] 8.2 Create `app/feedback.py` with `issue` (fails safely), `vote`, `reason`, `summary` and `prune`. Verify with tests: one vote per token with the last vote winning, a reason only after 👎, an unknown token refused, prune at 90 days, and `issue` returning `None` without raising when the table is unwritable.
- [x] 8.3 Add `writer.PROMPT_VERSIONS` and a test that hashes each prompt's text against a stored hash, so a prompt edit without a version bump fails. Set `verdict` to `v2`. Verify that `pytest tests/test_writer.py` passes.
- [x] 8.4 Attach a rating row to verdict cards (including forwarded-mail verdicts in Telegram), change alerts, the last `/watch` message and `/wrapped`, issuing tokens only for Telegram deliveries. Handle `fb:` and `fr:` taps with the button edits in design decision 9. Verify with bot tests: where the buttons appear (not on `/list` or help), the 👍 to Thanks and 👎 to reasons to Thanks flows, and that the stored row has no text, URL or member id.
- [x] 8.5 Add the feedback section to `metrics.report`, and call `feedback.prune` in the daily run. Verify with `tests/test_metrics.py` tests: the per-kind count, 👍 share and reasons; the per-prompt-version share; and "no feedback yet" when empty.

## 9. Integration

- [x] 9.1 Run the full suite and lint (`pytest` and `ruff check .`) and fix any failures.
- [x] 9.2 Deploy to the Sprite, call `POST /admin/telegram-webhook`, then check on a real phone: the empty-chat description, both command menus, joining by link, help buttons, the watch prompt, the "Watch X?" suggestion, the progress reaction and draft on a link and a PDF, unwatch and undo, an explain pop-up, expandable quotes, voting, and `/stats` feedback.

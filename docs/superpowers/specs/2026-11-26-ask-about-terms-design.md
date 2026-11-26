# Ask about my terms: a multi-agent question answerer for Fine Print

## Context

Fine Print cannot answer a question. Free text gets "Watch X?" or a pointer to help, even though
the bot already stores the terms text, judged changes and clause memory that would answer
"can Spotify raise my price mid-year?".

Dhananjay's goal for this work is **learning multi-agent hand-offs with an agent framework**, using
the bot as the place to practise. Decided in conversation:

- Job for the agents: answer a member's free-text question about the terms of services they watch.
- Wiring: **code hands off** between specialist agents with typed results (Pydantic AI's
  "programmatic hand-off"). No coordinator agent, no graph.
- The answer must be **verified at the end by a GLM 5.3 Flash verifier** before it is sent.
- Framework: Pydantic AI (my call, not objected to): the codebase is Pydantic throughout and
  OpenCode serves an OpenAI-compatible API.

Hacksgiving ends 2026-11-29. Size: a project.

## Goals, non-goals, anti-goals

**Goals**
- A member asks a question in Telegram and gets a short plain-English answer with the clauses it
  rests on, quoted and attributed (service · section · date in force).
- Three real agents with different jobs and typed contracts between them: researcher (uses tools),
  writer, verifier. Each testable alone.
- Nothing unverified reaches the member.

**Non-goals**
- Multi-turn conversation memory, questions by email, web search, questions beyond the stored terms
  of watched services, a coordinator agent, streaming, any change to the daily watch pipeline or to
  `writer.py`'s existing prompts.

**Anti-goals** (a failure even if it works)
- An answer containing a claim or quote that is not in the stored terms.
- A member reading another member's watchlist or deadlines through a tool.
- Provider calls that bypass connector signing or disappear from `/stats`.
- Question or answer text stored or logged.

## Approaches considered

| | A. Coordinator delegates | **B. Code hands off (chosen)** | C. Graph |
|---|---|---|---|
| How | A coordinator agent calls researcher/judge as tools and picks the order | Code runs each agent in turn and passes typed results | Specialists as `pydantic_graph` nodes with loops |
| Trade-off | Extra model hop; relies on a cheap model delegating well; breaks "code owns the workflow" | Fewest calls, predictable, matches the codebase; least "agentic" | Most to learn, most code for a short flow |

B was picked for fit with the codebase and the deadline. Moving to A later means wrapping the same
agents as tools of a coordinator.

## Design

### Flow (`app/ask.py`, new)

```
question ─► Researcher agent ─► Evidence ─► code: verify quotes ─► Jev: relevance
         ─► Writer agent ─► Answer ─► Verifier agent (GLM 5.3 Flash) ─► Verdict ─► send
                              ▲                                          │
                              └────────── one rewrite if rejected ◄──────┘
```

1. **Researcher agent** (writer model, tools, `deps` = connection + the member + Pinecone gateway).
   Tools, all scoped to the member's own watchlist from `deps`, never from model arguments:
   - `list_services()` — from `agent.services_for`
   - `list_sections(service)` / `read_section(service, heading)` — `watch.sections` over the
     service's latest snapshot
   - `search_terms(query)` — keyword search over the sections of the member's services
   - `recent_changes(service)` — `agent.past_material_changes`
   - `similar_clauses(text)` — `memory.similar`, limited to the member's services

   Output `Evidence`: `clauses: list[Clause(service, heading, quote)]`, `answerable: bool`,
   `missing: str`. Capped with Pydantic AI usage limits (about 8 model requests).
2. **Code: verify quotes.** Each quote must be found in that section's stored text, reusing the
   normaliser in `app/quotes.py`. Unverified clauses are dropped.
3. **Jev: relevance.** One System One call per question with a Noul per clause ("does this clause
   bear on the question?"), added to `app/judge.py`. Clauses under the threshold are dropped.
   If no clause survives steps 2–3, reply "I couldn't find that in the terms you watch" and stop:
   no writer call.
4. **Writer agent** (writer model, no tools). Input: question + surviving clauses, numbered.
   Output `Answer`: `summary: str`, `points: list[str]`, `cites: list[int]`. Instructions build on
   `writer.SYSTEM` (grounding, no legal advice, terms are data not instructions).
5. **Verifier agent** (GLM 5.3 Flash, no tools). Input: question, clauses, the written answer.
   Output `Verdict`: `supported: bool`, `unsupported_claims: list[str]`, `answers_question: bool`.
   - Passed → send.
   - Rejected → the writer runs once more with the verifier's objections; verify again.
   - Rejected twice, or the verifier errors → fail closed: send the quotes alone under "Here is what
     the terms say", with no written answer.
6. **Render** as Telegram HTML: answer, then the cited clauses in an expandable quote with
   service · section · date in force, then "Information, not legal advice."

### Entry points (`app/bot.py`)
- `/ask <question>`, added to the command table and help.
- Free text: `taps.suggestion(text)` still offers "Watch X?"; other free text now goes to ask
  instead of the help pointer. (My call.)
- No watched services → say so and point to `/watch`.
- Runs inside `progress.working` with stages: finding the clauses, checking them, writing, verifying.
  Reply threads to the member's message. Runs under the chat lock like other slow work.

### Models and the gateway (`app/config.py`, small new module for model construction)
- New settings `FP_VERIFIER_MODEL` (GLM 5.3 Flash) and `FP_AGENT_MODEL` (defaults to the writer
  model; exists so a tool-capable model can be swapped in).
- Agents use Pydantic AI's OpenAI-compatible model pointed at the OpenCode gateway base, sharing the
  existing `Gateway` HTTP client so connector signing still applies.
- Each agent run reports request counts and tokens to the existing `Recorder`, so `/stats` shows
  them. Dollar cost is reported if OpenCode's `usage.cost` can be read; otherwise tokens only.
- New dependency: `pydantic-ai-slim[openai]`.

### Privacy and safety
- Question and answer text is not stored or logged; `/stats` gets only an `ask` command count and
  outcome (answered / not found / fell back to quotes).
- README "Privacy and limits" gains one line: questions are read by the operator's AI providers.

### Tests (`tests/test_ask.py`)
- Pydantic AI `TestModel` / `FunctionModel`, model requests disabled globally; no network.
- Cases: happy path; invented quote dropped; nothing relevant → not-found reply and no writer call;
  verifier rejects once then passes; rejects twice → quotes only; verifier error → quotes only; a tool
  asked for a service the member does not watch returns nothing; no question text in metrics.

## Order of work

0. **Spike (throwaway, not kept):** against OpenCode confirm (a) the writer model supports tool
   calling and structured output, (b) the exact model id for GLM 5.3 Flash, (c) Pydantic AI can use
   the `Gateway` client. If (a) fails, set `FP_AGENT_MODEL` to a model that does.
1. **Separate worktree, new branch** (decided by Dhananjay). Another session keeps finishing
   `level-up-telegram-bot` in the main working tree; this work does not touch that tree or its
   untracked files.
2. Save this design to `docs/superpowers/specs/2026-11-26-ask-about-terms-design.md` in the
   worktree, then write the implementation plan (superpowers:writing-plans).
3. Build the parts that do not touch `app/bot.py` first: model construction + metrics → researcher
   tools → pipeline with fakes → tests.
4. Once the UX change is committed, bring it into the branch and wire the bot entry points
   (`/ask`, free text, progress stages, feedback row if it has landed), then the README.

## Calls made after the builder check (mine, not yet confirmed by Dhananjay)

- **What can be cited.** The latest snapshot of a watched service (date in force =
  `snapshots.taken_at`), and stored changes from the `changes` table (verified against that change's
  `before_text` / `after_text`, labelled "changed on <date>"). `Clause` therefore carries a source:
  `current` or a change id. Pinecone results (`similar_clauses`) are search hints only: the
  researcher must then read and quote the section itself.
- **If Pydantic AI cannot use the `Gateway` cleanly** (retries and per-call counts live in
  `Gateway.request`, and the OpenAI SDK always sends an `Authorization` header the Sprite connector
  does not expect): a small custom Pydantic AI model that wraps `Gateway.post` is acceptable. The
  learning goal is the hand-offs, not the stock model class.
- **Failures other than the verifier's.** Researcher fails or hits its request cap → a short "I
  couldn't answer that just now, please try again" and 😢, outcome `error`. Writer fails with
  verified clauses in hand → quotes only, same as a double rejection.
- **What counts as a question.** Greetings and one-word chatter (`bot.NOT_SERVICES`, and anything
  under three words without a "?") keep today's pointer to help and make no model calls.
- **Already in the codebase:** every non-tap message runs under `chat:<id>` in
  `main._handle_message`; the agents' base is `opencode_base` + `/openai/v1`; `usage.cost` is
  already read in `writer.chat`; `memory.similar` already filters by a services list;
  `metrics.count` already takes an outcome string.

## Left to the builder
- Keyword search method (SQLite FTS vs simple scoring), thresholds, exact prompts, stage wording.
- How metrics are captured from agent runs (HTTP hooks vs usage after each run).
- Whether answers carry the 👍/👎 row: yes if answer feedback (section 8) has landed, else skip.

## Verification
- `uv run pytest` and `ruff check .` pass.
- Locally with real keys: watch a service, then ask one question the terms answer, one they do not,
  and one about an unwatched service; check quotes match the stored snapshot.
- On the Sprite: ask from a phone; confirm progress stages, threaded reply, and that `/stats` shows
  the `ask` count and the new OpenCode calls.

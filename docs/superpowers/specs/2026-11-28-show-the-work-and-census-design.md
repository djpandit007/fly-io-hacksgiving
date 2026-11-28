# Fine Print: show the work, and a census (design)

Design for the last 48 hours of Fly.io Hacksgiving, approved by Dhananjay on 28 Nov 2026.
Size: a project, in four lanes.

## Context

Fine Print's engine is already deep: daily watch, Jev's typed judgments, verified quotes, Scout and
Advocate Sprites, approved opt-outs, redline kits. What is missing is everything a judge would see.

- **Deadline:** Sun 29 Nov 2026, 11:59 PM Pacific = **Mon 30 Nov, 1:29 PM IST**.
- **The form wants:** a code link, a demo link, one or two sentences, which Sprites connectors were
  used, and a social post with a short demo that tags @flydotio. No judging criteria are published.
- **Judges** come from TypeSafe, Box, Fly.io, Pinecone, Ollama and Apify. Prizes: $2,500, $1,500, and
  $1,000 for "the strangest agent".

**Decided with Dhananjay**

1. Judges watch a 2–3 minute video; the join link is a bonus for the curious.
2. Day one makes the existing work visible; day two builds a census of popular services.
3. The video shows the opt-out notice Fine Print drafts and leaves sending to the member. Nothing is
   sent from the app on camera.
4. The demo company lives on a free shared host. That costs the "terms contradict the privacy
   policy" alert in the video, because Fine Print does not attach companion pages on shared hosts.
5. Building continues into Monday morning.

**What a judge cannot see today** (verified in code)

- Nothing shows a terms change on demand; an alert only happens when a live page really changes.
- Scout evidence screenshots are saved and never shown (`snapshots.evidence_path` is write-only).
  The word diff (`ChangeReport.redline`) is computed and never rendered. Past-year cards from
  `/watch` have no buttons. The swarm appears only as totals in the owner's `/stats`.
- Benchmarks need 10 public services per category (`benchmark.MIN_COHORT`) and the index only holds
  material changes of watched services, so the 📊 line is almost certainly dark.
- Wrapped is one text message. The repo has no screenshots, video, script or licence.

## What gets built

| Lane | What a judge sees |
|---|---|
| 1. Show the work | A change detected on camera; buttons for the diff, the evidence screenshot and how it was judged; the Sprites narrating their own steps; a notice ready to send |
| 2. Wrapped card | A shareable image of your year in fine print (also the social post) |
| 3. Census | "More severe than 86% of 74 services" on alerts and verdict cards; `/radar`; watch everything you use in one message; Jev on the TypeSafeAI connector; Apify crawling |
| 4. Guests and kit | A good first minute for a judge who joins; demo company, video script, README, licence, post copy |

## Goals, non-goals, anti-goals

**Goals**

1. The video can show, for real and on demand: a terms change detected, the alert, its evidence and
   reasoning, an opt-out notice drafted and ready to send; a contract checked and redlined; the card.
2. A judge who joins gets something worth seeing in the first minute, with nothing to wait for, and
   cannot run up the operator's bill.
3. The 📊 benchmark line appears on alerts and verdict cards.
4. The submission has its assets: script, README, licence, post copy, form answers.

**Non-goals**

- No web UI. No storing members' documents. No per-member Sprites.
- No simulated, replayed or reset path in the product: the demo runs the real pipeline, and takes
  are staged by redeploying the demo site.
- Sending from the app (Approve & Send, receipts, its limits) is untouched and not in the video.
  `/receipts` is not built: no judge could reach a sent opt-out.
- Census services are graded, not watched: no history, no daily checks, no contradiction search.
- No changes to thresholds, prompts, the worker runners or the safety checks in `app/discover.py`.
- OpenSpec capability specs are not updated before the deadline (my call; this design is the record).

**Anti-goals** (failure even if it all works)

- A new feature destabilises the daily watch before recording.
- The video or README claims something the code does not do ("300 ms restore", "forks Sprites",
  "HMAC-signed"). The measured restore is ~15 s warm and ~48 s cold; say that.
- A named real company is graded wrongly in public.
- A new button shows one member another member's data.
- No video exists at the deadline because building ran late.

## Constraints

- One live environment (Gateway, Scout, Advocate), so live testing is serial. `main` is protected:
  every lane lands by PR with `pytest` and `ruff` green.
- Other agents may be working in this repo: commit with explicit paths, never `git add -A`, and leave
  untracked files you did not create alone.
- Telegram: button data ≤ 64 bytes, messages ≤ 4096 characters, captions ≤ 1024, photos ≤ 10 MB with
  width + height ≤ 10,000.
- A slow tap (`taps.SLOW`) strips every button from its message before it runs (`app/main.py`), so
  new taps must stay out of that set.
- A worker job costs about 5 s of settle time plus a 15–48 s restore. The connector gateway may time
  requests out at about 120 s (unverified), so long provider jobs must be started and polled.
- Jev takes 32k tokens per question; documents are capped at `judge.MAX_DOCUMENT_CHARS`.
- Existing quotas bind the owner too: 5 redline kits and 10 Scout link checks per UTC day.
- Existing privacy rules hold: metrics and `swarm_events` carry no URLs or member content; nothing is
  kept from a member's own link or file; cost is only ever what a provider reported.

## Approaches considered

Day one (show the work) is common to all three. For day two:

- **A. Polish only.** Safest, but benchmarks stay dark and the first minute for a judge is slow.
- **B. Census (chosen).** The only option that adds a moment to the video *and* makes the bot instant
  for a stranger, and it puts Pinecone, Apify and TypeSafe on screen doing real work. It is the lane
  most likely to slip, but it is additive: 30 services still works and nothing else depends on it.
- **C. All partner connectors.** Aims at the panel, but four live integrations, and benchmarks stay
  dark. Its two best pieces (Ollama verifier, Box evidence locker) are kept as stretch.

## Lane 1: show the work

**One migration for the whole change, landed first** (`app/db.py`, `SCHEMA_VERSION` 7 → 8): add
`snapshots.provenance TEXT` (JSON, NULL for old rows) and the `census` table from Lane 3. Lanes 2–4
build on it, so no two lanes race for a schema version.

**1.1 `/check <service>` (owner only).** Move the per-service body of `agent.tick` (run
`check_bundle`, build `alert()` and `contradiction_alert()` for each watcher, with the shared
vector and benchmark caches) into one function. `tick` loops over it; `/check` calls it for one
watched service inside its own progress bubble, then `notify.deliver`; and it replaces the copy in
`app/intake.py`, which today alerts without benchmarks. The owner gets a one-line result ("Checked
Gobblr: 1 material change, 2 members notified", or "No changes since 27 Nov"). Add it to
`OWNER_MENU`; the command list derives from the menus.

**1.2 The swarm narrates itself.** `Swarm.job`, `run_job` and `scout` (`app/swarm.py`) take an
optional `on_stage` (the existing `progress.Stage` type) and call it at each step: "🔒 Locking the
Scout's network to gobblr.netlify.app…", "🌐 Reading the page in a quarantined browser…", "⏪ Rewinding
the Scout to its clean checkpoint…". Cosmetic only: it must never raise or change an outcome.

- `/watch`, link checks and file checks already run inside a bubble, so passing `on_stage` down
  through `page_text`, `check_bundle` and `check_url` is enough for them.
- The redline-kit tap runs bare today. Give it a bubble and thread `on_stage` through
  `advocate.redline_kit`, whose three jobs are the longest wait in the product.
- Leave the Approve & Send tap alone.

**1.3 Provenance.** `page_text` reports how it read the page (direct, Scout, Exa, LlamaParse).
`Swarm.job` today times only the lock and the restore and returns neither, so add a small trace
object that it fills with the lock, run and restore times and the number of allowed hosts.
`record_version` stores that JSON on the snapshot, with the judge's model id, which is not stored
anywhere today. It describes a public page fetch: no member data.

**1.4 Evidence for every material change.** In `check_bundle`, when a live version produced a
material change and has no screenshot (the direct fetch worked, so the Scout never ran), run one
Scout job with `screenshot=True`, save it with `swarm.save_evidence` under that snapshot's digest,
and update the snapshot's `evidence_path` and provenance (`record_version` only sets the path at
insert). Record the capture time, the PNG's SHA-256 and whether the Scout's text matched the judged
text. Best effort: a failure is logged and the alert goes out without the button. Never for
members' links.

**1.5 Inspector buttons** on every change card (`agent.alert`), with new button data in
`app/taps.py` (grammar comment, `_valid`, `METRIC`; none of them in `SLOW`):

```
Gobblr changed Dispute Resolution (28 Nov 2026)
arbitration · Major (2.7/3) · favours the company
…
[🔍 What changed]  [📸 Evidence]  [🔬 How I checked]
[✉️ Review & Draft Opt-Out]
```

- `cd:<change id>` sends the diff: `watch.redline` output with removals struck through and additions
  bold, escaped, in an expandable quote, cut to one message.
- `ce:<change id>` sends the screenshot as a file (full-page captures are too tall for a Telegram
  photo) with a caption: service, document, capture time, text SHA-256, PNG SHA-256, and a plain
  note if the Scout's text differed. The button is shown only when a screenshot exists.
- `ch:<change id>` sends the reasoning, from stored fields only (no provider call). The model name
  is shown only for versions recorded after this change.

```
🔬 How I checked this
Read 28 Nov 2026 08:14 UTC, directly from gobblr.netlify.app
Screenshot by the Scout Sprite: network locked to 1 host (1.1 s),
page rendered (8.4 s), rewound to its clean checkpoint (15.2 s)
Jev's answers (jev-1.14):
• Would you want to know? yes, 96% sure
• About: arbitration   • Favours: the company
• How much it matters: Major (2.7 of 3)
• A way to opt out? yes, 93% sure
I alert when Jev is at least 70% sure a change matters.
```

- `co:<change id>` sends any stored change as a full card (with a benchmark cache, so the 📊 line
  can appear), and `cl:<service id>` lists a service's material changes of the past year as
  buttons. The `/watch` reply gets a `🔎 Open a change` button. Review & Draft Opt-Out is a slow
  tap, so it strips the row above it; `co:` is how a member gets the card back.
- Every one of these checks that the tapping member watches that service, with the join
  `advocate.draft_change` uses; otherwise it answers "That button no longer works."

**1.6 A notice the member sends themselves.** When Fine Print will not send an email opt-out (the
address is not on the service's own verified domain), today's reply opens with a warning and "Print
this notice, sign it and send it" (`advocate._printable`). For an email target, make it read as a
finished draft. Postal stays "print, sign and post"; a form says "fill it in yourself" and lists the
fields. The notice's last line says "Drafted with Fine Print" rather than "Sent at my request by
Fine Print". This is what the video shows.

```
Your opt-out notice for Gobblr is ready
⏰ Deadline: Mon 28 Dec 2026 (30 days)
Send it from your own email.
To: optout@gobblr.example
Subject: Opt-out notice: Gobblr
┌ To Gobblr, I am writing to opt out as your terms permit. …
I haven't sent this. I only send for you when the address is on the
service's own verified domain, so check this one before you use it.
```

**1.7 Close the API pages.** `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` in
`app/main.py`, with a test that all three return 404.

**1.8 Names from the registrable domain.** `bot._name_for` takes the second-to-last label of the
host, so `bbc.co.uk` and `amazon.co.uk` are both named "Co", and a page on `gobblr.netlify.app` is
named "Netlify" (checked by running it). Use the first label of `discover.registrable_domain(host)`
instead: "Bbc", "Amazon", "Gobblr"; `youtube.com` and `policies.google.com` are unchanged. Stored
names of existing services do not change. The demo company depends on this.

## Lane 2: Wrapped card

- Stats in plain SQL and Python, no provider call: services and documents watched, words in their
  current versions, hours to read them at 238 words a minute, material changes in the past year and
  the top three of Jev's categories, opt-out deadlines tracked, the harshest change.
- `app/card.py` renders a 1080 × 1350 PNG with Pillow on the Gateway, using bundled open-licence
  fonts. It works in single-Sprite mode and adds no worker wait (my call; the builder may revisit).
- `/wrapped` sends the card (`Telegram.send_photo`, new, beside `send_document`), then today's
  written dissent as before. If rendering fails, the text goes out alone.
- The line on the image is fixed ("I respectfully dissent."), not model output.
- When the census exists and at least five of the member's services are in it, one comparison line
  is added ("Your services force arbitration more often than most: 79% against 61%").
- **The look is Dhananjay's to pick** from two or three mockups, shown as soon as the build starts.
  If he has not picked by Sunday noon IST, the builder's first mockup is used.

```
AGREED WRAPPED 2026
You agreed to 412,000 words across 14 services.
Reading them would take 29 hours.
37 changes that mattered · 9 arbitration · 6 data · 5 renewal
4 opt-out deadlines caught
"I respectfully dissent."
```

## Lane 3: census

**3.1 Seed list.** About 80 well-known consumer services with their terms URL and, where known,
privacy URL, in a data file. It includes Fly.io and the five partner companies; whether the video
features them is decided after their results are spot-checked. A URL that fails a sanity check (too
short, or not a terms or privacy page) falls back to `discover.find_terms_url`.

**3.2 Reading the pages.** New `app/apify.py`: start one run of Apify's website content crawler over
all seed URLs, wait for it, read the dataset, validate with Pydantic, and report the run's cost to
metrics. Any URL Apify did not return goes through `agent.page_text` with the Scout skipped (160
Scout jobs would take hours). Without an Apify connector the whole census uses `page_text`.

- Start: `POST v2/actors/apify~website-content-crawler/runs`, JSON body
  `{"startUrls": [{"url": …}], "maxCrawlDepth": 0, "maxCrawlPages": <n>}`, with
  `maxTotalChargeUsd=2` so a run can never cost more than two dollars.
- Wait: `GET v2/actor-runs/<id>?waitForFinish=60`, repeated until `data.status` is `SUCCEEDED`,
  `FAILED`, `TIMED-OUT` or `ABORTED`. Each call stays under the gateway's time limit. Give up after
  20 minutes and fall back.
- Read: `GET v2/datasets/<data.defaultDatasetId>/items?format=json&clean=true`; each item has `url`,
  `crawl.loadedUrl`, `text` and `markdown`. Cost is `data.usageTotalUsd`, re-read ten seconds after
  the run ends because the first figure is preliminary.
- Apify's own estimate is $0.50 to $5 per 1,000 pages with a browser, so about 160 pages should cost
  well under a dollar; the free plan includes $5 a month.
- Write it against these documented calls with the provider faked, then check the connector's
  allowed paths against its "API Usage" example as soon as the connector exists.

**3.3 Grading, with Jev only.** For each document:

1. `judge.red_flags` with the battery for its role (`consumer_tos` for terms, `privacy_and_ai` for
   privacy). A document longer than Jev's limit is graded in windows and the highest answer per flag
   is kept, so a clause near the end is not missed.
2. For each flag found, code picks up to three candidate sections (the section splitter in
   `app/watch.py`) by keyword overlap with a small word list per flag. `agent.CATEGORY_WORDS` is per
   category and too coarse: all six privacy flags share "data".
3. One `judge.nouls` call confirms which section states each flag, and asks of each whether it
   offers a way to opt out. The section's own text is the clause, so nothing needs quote-checking.
4. One `judge.clause_severity` call scores the located clauses.
5. Each clause is upserted to Pinecone with `memory.remember` and `benchmark.clause_metadata`
   (`public`, `scale: clause`, category from `playbooks.FLAG_CATEGORY`, the opt-out answer, and a
   new `site` field holding the registrable domain), under a `census-…` id.

That is about three Jev calls and up to six embeddings per document, and no chat model.

**3.4 Storage.** A `census` table keyed by registrable domain, document role and flag: name, URL,
Jev's probability, the clause (or NULL when no section was confirmed), its severity, the text's
SHA-256 and when it was graded. Every flag gets a row, found or not, so shares can be computed. It
is separate from `services` and `snapshots` on purpose: the census must not create fake history or
interfere with how a watched service is named. `services.domain` holds the terms page's host, not
its registrable domain, so a member's services are matched in Python with
`discover.registrable_domain`, never by a plain SQL join.

**3.5 Running it.** Owner commands:

- `/census` shows status. `/census run` grades in the background under a Sprite hold, a few
  services at a time, skipping documents whose text has not changed, reporting progress every ten
  services and a final summary with failures. Capped at 100 services a run. Cost shows in `/stats`.
- `/census drop <domain>` removes a wrongly graded service: delete its rows and upsert its vectors
  again with `public: false`, which benchmarks already filter out. (Deleting through the Pinecone
  connector is unproven; upserting is.)

**3.6 Where it shows up.**

- The 📊 line: raise `benchmark.TOP_K` (50 today) so a cohort can cover the census, exclude the
  service's own census clause by `site` as well as by name, and add the three gentlest services of
  the cohort ("Gentler: Tidal, Deezer, Qobuz") in place of the unused `comparable_services`.
- `/radar` (member command):

```
📡 Fine Print census · 82 services, graded 29 Nov 2026
• Can change the terms without asking: 91%
• Auto-renews: 74%
• Binding arbitration: 61%
• Shares or sells your data: 44%
[Arbitration] [Auto-renewal] [Data sharing] …
```

  A flag button lists the five harshest and five gentlest services for it, ranked only over clauses
  that were located and scored, so an unreadable page is never called gentle. `/radar <service>`
  shows that service's standing flags with the clauses, and a button to watch it.
- `/watch` on a census service says what its terms say today straight away, then reads the past
  year as it does now.
- **Watch several at once.** `/watch spotify, netflix, uber` or "I use Spotify, Netflix and Uber"
  offers "Watch these 3?" with one Yes. The names wait in a RAM-only offer (like the opt-out offers
  in `app/advocate.py`), since button data cannot carry them; a restart or a Sprite sleep empties
  it, so an expired offer says so. Each service is watched in turn with the progress bubble counting
  ("2 of 3: Netflix…"), and one closing message sums up the year across all of them with a button
  per service to open its changes.

Two limits to say plainly rather than fix: categories the consumer batteries never produce (fees,
liability) get no 📊 line, so the lease in the video has none; and `/watch` replays of past changes
stay without it, to avoid a Jev call per old change.

**3.7 Jev through the TypeSafeAI connector.** A `typesafe` provider in `app/config.py`
(`FP_TYPESAFE_BASE`, key for local use). `Clients` gets a judge gateway that is TypeSafe when
configured and OpenCode otherwise; `app/judge.py` picks the path for each (`v1/systemone` against
`systemone/v1/systemone`); every Jev call site uses it. Unset means today's behaviour exactly. The
day-one client (`git show adf4ec7:app/judge.py`) used model `jev-latest` and sent a long-context
header on whole-document calls only; confirm both against TypeSafe's API once the connector exists.

**3.8 History for names with a capital inside (a bug found while planning).** `history.ota_versions`
asks the Open Terms Archive for `<service name>/Terms of Service.md`, and the name comes from
`bot._name_for`, which gives "Youtube" and "Linkedin". The archive's folders are "YouTube" and
"LinkedIn" and the lookup is case-sensitive (checked live on 28 Nov: 3 versions against 0), so these
services silently fall back to eight Wayback captures. Fix: list each archive repo's top-level
folders once a day (one GitHub call each, cached) and match the name without regard to case. The
unauthenticated GitHub limit is 60 calls an hour, so keep the per-document calls as they are.

## Lane 4: guests and the submission kit

**4.1 Limits.** For every member except the owner, per UTC day (`app/quota.py`): 5 new services
onboarded, 15 documents or links checked, 30 questions. Services someone already watches stay free
and instant. No more than 50 members in all (`users.join_member`). The join link goes in the form's
notes for judges, not in the README or the public post, so spend stays bounded without a global
meter. Each message at a limit says so plainly.

**4.2 A real alert for a newcomer.** `FP_SHOWCASE_SERVICE` names the demo company. The welcome and
`/help` get a `🎬 See a real alert` button that adds it to the member's watchlist and sends its
newest material change that offers an opt-out as a full card, with one line saying this is a
fictional demo company. Every button on it works, and the opt-out comes back as a draft. Unset hides
the button.

**4.3 Welcome copy** offers three things: see a real alert, tell me what you use, send me a contract.

**4.4 The demo company** (`demo/site/v1/`, `demo/site/v2/`, `demo/switch.sh`, `demo/README.md`):

- A static site for a fictional service with a one-word name, each folder complete, with a footer
  saying it is fictional. Terms v1 is mild. Terms v2 adds binding arbitration with an opt-out
  "within 30 days" by email, a licence to train AI on what users upload, and a price change on
  renewal. The opt-out address is on the reserved `.example` domain, so nothing anyone sends can
  reach a real mailbox.
- Hosted on any free static host where you choose the subdomain (Netlify, Cloudflare Pages). Not a
  project page under a personal GitHub account: the name would come out as the account's.
- **Takes.** `demo/switch.sh v1|v2` stamps a "Last updated" time into the page and redeploys that
  folder. The stamp gives every deploy a new digest, so each take gets a fresh screenshot and the
  full narration; without it, a repeated v2 reuses the stored screenshot and the Scout never runs.
  One take is: switch to v1, `/check`, switch to v2, `/check`.
- **Rules for takes.** Rehearse and record before the join link is shared, and do not touch the
  site afterwards, because going back to v1 is itself a change that every watcher is told about.
  Pause the daily workflow while recording (it runs at 7:47 PM IST and would use up the change).
  The redline kit runs five times a UTC day, so rehearse it sparingly.

**4.5 Video script** (`docs/demo/script.md`): a 2:30 run with every tap and line, what to stage
beforehand, and honest numbers. Tap the inspector buttons before Review & Draft Opt-Out.

| Time | Shot |
|---|---|
| 0:00 | Hook and the Wrapped card |
| 0:15 | "I use Spotify, Netflix, Uber and Notion": all watched, the year summed up |
| 0:35 | The demo company changes its terms; `/check`; the Sprites narrate; the alert with its 📊 line |
| 0:55 | Diff, evidence, how it was judged |
| 1:10 | Review & Draft Opt-Out: the notice, the clause quoted, the deadline in the calendar; you send it |
| 1:30 | A lease: verdict, a clause void where you live, the redline kit |
| 1:55 | How it is built: three Sprites, connectors with least privilege, Jev's typed answers |
| 2:15 | `/radar`, the card again |

**4.6 README.** Lead with the pitch, screenshots from the recording, a "For judges" section (three
things to try; the join link is in the submission notes), a table of who does what, and a diagram
GitHub renders. The setup reference stays below. Correct any claim the code does not back.

**4.7 `LICENSE`** (MIT unless told otherwise) and **4.8 `docs/demo/submission.md`**: the two
sentences, the connectors answer, notes for judges with the join link, and post copy.

## Stretch, decided Sunday evening once everything above is live

1. The `/ask` verifier on Ollama Cloud: a second opinion from another vendor's open-weight model.
   `llm.model_for` takes the provider's API prefix; unset falls back to OpenCode.
2. Box as an evidence locker: each screenshot and a Markdown record of the change uploaded through a
   connector that can upload but not delete. The upload half of `app/box.py` is in git at `0e553ba^`.

## Tasks only Dhananjay can do

1. **Today:** TypeSafeAI and Apify connectors in the Fly dashboard (name prefix
   `fly-io-hacksgiving`, label `fineprint`), and their gateway bases in `.env.sprite`. Lane 3 is
   written against the documented APIs either way, but it can only be verified once they exist. If
   either is not possible, say so: Jev stays on OpenCode, or the census uses the existing fetchers.
2. Put the demo company on a free static host under a subdomain of his choice.
3. Pick the card's look from the mockups.
4. Deploy with `scripts/setup_sprite.sh`; run `/census run`; spot-check ten graded services.
5. Record, post, submit.

## Order, deadlines and what gets cut

- **Sat 28 Nov:** the migration lands first. Then lanes 1, 2 and 4 in parallel worktrees, and lane 3
  started at the same time because it carries the most risk. Lane 1 merges before the others touch
  `app/bot.py` and `app/agent.py`.
- **Sun 29 Nov:** merge, deploy, census run, live testing. Stretch decided in the evening.
- **Sun night: a safe take.** Record the whole script with whatever is live, so a video exists
  whatever Monday brings (my call, given the choice to build into Monday).
- **Mon 30 Nov:** last merge at 9 AM IST. Deploy, one rehearsal, final recording by 11 AM, post and
  submit by 12:30 PM. The deadline is 1:29 PM.

If time runs short, cut in this order: stretch; the Wrapped comparison line and `/radar <service>`;
the archive name fix (3.8); watch several at once; Apify (the census falls back to `page_text`); the
TypeSafeAI switch; the showcase button; the open-a-change list. Never cut: `/check`, the inspector
buttons, the narrated steps, the send-it-yourself draft, the name fix (1.8), the card, the demo
company, the script, README and licence, the closed API pages, and the limits.

## Verification

- Per lane: `uv run pytest -q` and `uvx ruff@0.17.6 check .` green; new tests beside the old ones for
  button validation and forged ids, access checks on every new tap, provenance stored, evidence
  capture failing quietly, the send-it-yourself draft for email, post and form targets, service
  names for country-code and shared-host domains, limits and the member cap, census grading with
  Jev, Pinecone and Apify faked, a census service never benchmarked against itself, name parsing
  for several services, the card being a valid PNG of the right size, and the closed API pages.
- `tests/test_e2e_smoke.py` grows: the alert carries the inspector buttons and they answer only a
  watcher; a faked census of twelve services makes the 📊 line appear.
- Live, by the owner on the Sprites: `demo/switch.sh v2` then `/check`; the alert arrives under the
  company's own name with the narration; `📸` returns a PNG; `🔬` shows the Scout's timings; Review
  & Draft Opt-Out returns the ready-to-send notice; a second take after `switch.sh v1` and `v2`
  narrates again; `/wrapped` sends the card; `/census run` finishes and `/radar` shows shares; a
  second Telegram account joins, taps "See a real alert", and is stopped at its sixth new service.
- The safe take on Sunday night is itself the end-to-end test of the script.

## Left to the builder

Wording of new messages; button layout; the per-flag word lists and how candidate sections are
scored; the card's layout within the chosen look; Apify's actor input beyond the fields above;
whether `page_text` reports its path by return value or an out-parameter; concurrency; test
structure; the demo company's name and which static host.

## Calls I made (check these while reading)

1. One migration for the whole change, landed before anything else.
2. No reset or replay in the product. Takes are staged by redeploying a time-stamped demo site, and
   recorded before the join link is shared.
3. `/receipts` is dropped, and the email draft is reworded to read as ready to send.
4. Service names come from the registrable domain (this also fixes every `.co.uk` being "Co").
5. The census is separate from `services` and `snapshots`, and is graded, not watched.
6. The census asks only Jev; a clause is always the stored section text.
7. Evidence is sent as a file with its capture time and both hashes, and says when the Scout's text
   differed from the text that was judged.
8. Limits apply to every member except the owner; membership is capped at 50; the join link goes in
   the form's notes, not in the README or the post.
9. The card is drawn with Pillow on the Gateway, with a fixed line rather than model text.
10. `/check` and `/census` are owner-only.
11. The census skips the Scout: Apify, then direct fetch, Exa and LlamaParse.
12. A safe take on Sunday night, and the last merge at 9 AM on Monday.
13. The demo's opt-out address is on `.example`, so a judge who sends the draft reaches no one.
14. No OpenSpec artifacts for this change.

## Building it

The implementation plan is written lane by lane under `docs/superpowers/plans/`, and the lanes are
built in parallel worktrees. Implementation agents run on Opus 5.5 (Dhananjay's instruction).

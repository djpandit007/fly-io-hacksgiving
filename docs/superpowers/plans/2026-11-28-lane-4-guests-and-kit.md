# Lane 4: Guests and the Submission Kit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a judge who joins a good first minute without letting strangers run up the bill, build the demo company the video is recorded on, and write the script, README, licence and submission notes.

**Architecture:** Three small fixes the demo depends on come first (service names, closed API pages, a draft the member sends themselves). Then limits and a member cap, a "see a real alert" button on a fictional demo company hosted as a static site, and the written kit. The demo runs the real pipeline: a take is staged by redeploying the site with a new revision stamp.

**Tech Stack:** Python 3.12, FastAPI, SQLite, pytest (asyncio auto mode), static HTML, bash.

**Spec:** `docs/superpowers/specs/2026-11-28-show-the-work-and-census-design.md`, sections 1.6 to 1.8 and Lane 4.

## Global Constraints

- All of `docs/superpowers/plans/2026-11-28-show-the-work-and-census.md` "Global Constraints" apply.
- Lane 0 is already on your branch: `settings.showcase_service`, the button data `h:demo`, and `FakeTelegram`'s recorders and `watch()` in `tests/test_bot.py`.
- `advocate.approve`, the approval tokens, the send limits and the preview for a verified domain are not changed. Task 3 changes only what a member sees when Fine Print will **not** send.
- The checks in `app/discover.py` (`USER_CONTENT`, `is_user_content`) are not changed: the demo company's opt-out must come back as a draft because of them.
- The demo company is plainly fictional on every page, and its opt-out address is on the reserved `.example` domain.
- The join link never appears in the README or in post copy; it goes in the form's notes for judges.
- Docs never claim a 300 ms restore, forked Sprites or HMAC-signed tokens.

## Review Focus

1. **A join link that leaks.** Expected: member 51 is told the bot is full; a member's sixth new service of the day is refused; the owner is never limited. (Task 4)
2. **The showcase button before the demo exists, or after the demo's newest change is a removal.** Expected: a plain "isn't ready yet", and otherwise the newest change that offers an opt-out. (Task 5)
3. **Country-code and shared-host domains.** Expected: `amazon.co.uk` is "Amazon", `gobblr.netlify.app` is "Gobblr", and existing names are unchanged. (Task 1)
4. **A second take.** Expected: a new revision stamp changes the page's digest without counting as a change. (Task 6)
5. **A postal opt-out, or a form on an unverified domain.** Expected: postal still says print and sign; the form says fill it in yourself. (Task 3)

---

### Task 1: Service names from the registrable domain

**Files:**
- Modify: `app/discover.py` (new `display_name`), `app/bot.py` (`_name_for`), `app/intake.py` (`_terms_update` names the service with it)
- Test: `tests/test_discover.py`, `tests/test_bot.py`

**Interfaces:**
- Produces: `discover.display_name(host: str) -> str`: the first label of `registrable_domain(host)` with a leading `www.` removed, capitalised with `str.capitalize`. `bot._name_for(host)` returns it.

Today `_name_for` takes the second-to-last label, so `bbc.co.uk` and `amazon.co.uk` are both "Co" and a page on `gobblr.netlify.app` is "Netlify"; `intake._terms_update` takes the first label, so `email.spotify.com` is "Email". Names already stored do not change.

- [ ] **Step 1: Write the failing tests**

`tests/test_discover.py`:

```python
@pytest.mark.parametrize("host,name", [
    ("www.youtube.com", "Youtube"), ("policies.google.com", "Google"), ("bbc.co.uk", "Bbc"),
    ("www.amazon.co.uk", "Amazon"), ("gobblr.netlify.app", "Gobblr"), ("gobblr.pages.dev", "Gobblr"),
    ("email.spotify.com", "Spotify"), ("t-mobile.com", "T-mobile"), ("notion", "Notion"),
])
def test_display_name_comes_from_the_registrable_domain(host, name):
    assert discover.display_name(host) == name
```

`tests/test_bot.py`:

```python
def test_services_on_country_code_and_shared_hosts_get_their_own_name():
    assert [bot._name_for(h) for h in ("www.amazon.co.uk", "gobblr.netlify.app", "www.youtube.com")] == ["Amazon", "Gobblr", "Youtube"]
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_discover.py -k display_name tests/test_bot.py -k country_code`
Expected: FAIL (`AttributeError: module 'app.discover' has no attribute 'display_name'`).

- [ ] **Step 3: Implement** `display_name`, and use it in `_name_for` and in `intake._terms_update`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/discover.py app/bot.py app/intake.py tests/test_discover.py tests/test_bot.py
git commit -m "Name services from their registrable domain"
```

---

### Task 2: Close the API pages

**Files:**
- Modify: `app/main.py:91`
- Test: `tests/test_main.py`

- [ ] **Step 1: Write the failing test**

```python
def test_the_api_pages_are_closed():
    with TestClient(app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404
```

- [ ] **Step 2: Run and see it fail**

Run: `uv run pytest -q tests/test_main.py::test_the_api_pages_are_closed`
Expected: FAIL (`assert 200 == 404`).

- [ ] **Step 3: Implement:** `app = FastAPI(title="Fine Print", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_main.py
git commit -m "Close the public API pages"
```

---

### Task 3: A draft the member sends themselves

**Files:**
- Modify: `app/advocate.py` (`notice_body`, `finish`; new `_self_send`)
- Test: `tests/test_advocate.py`

**Interfaces:**
- Produces:
  - `advocate.notice_body(offer, fields, today=None, *, self_send: bool = False) -> str`: with `self_send` the last line is `(Drafted with Fine Print, an assistant I use to manage the terms I agree to.)`; without it the text is unchanged.
  - `advocate._self_send(offer: Offer, fields: dict[str, str]) -> Reply`, used by `finish` where it calls `_printable` today for an email or form target that fails `_domain_ok`. A postal opt-out still goes through `_printable`.

Exact copy. `{deadline}` is what `_deadline(offer)` returns (a line ending in a new line, or nothing). Everything from the member or the document is escaped.

Email:

```
<b>Your opt-out notice for {service} is ready</b>
{deadline}Send it from your own email.
To: <code>{target}</code>
Subject: <code>Opt-out notice: {service}</code>

<pre>{body, with self_send=True}</pre>

I haven't sent this. I only send for you when the address is on the service's own verified domain, so check this one before you use it.

<i>Information, not legal advice.</i>
```

Form:

```
<b>Your opt-out for {service} is ready to fill in</b>
{deadline}Fill in the form yourself: {form url}
• {field}: {value}

I haven't submitted this. I only submit for you when the form is on the service's own verified domain, so check it before you use it.

<i>Information, not legal advice.</i>
```

- [ ] **Step 1: Write the failing tests** in `tests/test_advocate.py`

```python
INSTRUCTION_30 = "You may opt out by emailing optout@gobblr.example within 30 days."


def ada(conn):
    user = users.join_member(conn, "join-code", chat_id=1, name="Ada Lovelace")
    conn.execute("UPDATE users SET email = 'ada@example.com' WHERE id = ?", (user.id,))
    return users.by_id(conn, user.id)


def offer(channel="email", target="optout@gobblr.example", domain=None):
    opt = OptOut(channel=channel, target=target, deadline_days=30, verbatim_instruction=INSTRUCTION_30)
    return advocate.Offer(service="Gobblr", domain=domain, opt=opt)


def test_an_unverified_email_opt_out_reads_as_a_finished_draft(conn):
    reply = advocate.finish(conn, ada(conn), offer())
    assert not reply.buttons and reply.text.startswith(
        "<b>Your opt-out notice for Gobblr is ready</b>\n⏰ Deadline: within <b>30 days</b>\n"
        "Send it from your own email.\nTo: <code>optout@gobblr.example</code>\nSubject: <code>Opt-out notice: Gobblr</code>\n\n<pre>To Gobblr,")
    assert "(Drafted with Fine Print, an assistant I use to manage the terms I agree to.)" in reply.text
    assert "Sent at my request" not in reply.text and "Print this notice" not in reply.text
    assert reply.text.endswith(
        "I haven't sent this. I only send for you when the address is on the service's own verified domain, "
        "so check this one before you use it.\n\n<i>Information, not legal advice.</i>")


def test_a_verified_email_opt_out_still_offers_to_send(conn):
    reply = advocate.finish(conn, ada(conn), offer(domain="gobblr.example"))
    assert [label for label, _ in reply.buttons[0]] == ["✅ Approve & Send", "✖ Cancel"]
    assert "Review your opt-out notice for Gobblr" in reply.text and "Sent at my request by Fine Print" in reply.text


def test_an_unverified_form_says_fill_it_in_yourself(conn):
    reply = advocate.finish(conn, ada(conn), offer("web_form", "https://forms.gobblr.example/opt-out"))
    assert not reply.buttons and reply.text.startswith("<b>Your opt-out for Gobblr is ready to fill in</b>")
    assert "Fill in the form yourself: https://forms.gobblr.example/opt-out" in reply.text
    assert "• Name: Ada Lovelace" in reply.text and "I haven't submitted this." in reply.text


def test_a_postal_opt_out_still_says_print_and_sign(conn):
    reply = advocate.finish(conn, ada(conn), offer("postal", "Gobblr Legal, 1 Main Street, Springfield"))
    assert "Print this notice, sign it and send it to:" in reply.text and "Sent at my request" in reply.text
```

Use the file's existing `conn` fixture. Update any existing test that pinned the old warning for an unverified email or form.

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_advocate.py -k "finished_draft or fill_it_in or print_and_sign or still_offers"`
Expected: FAIL on the first and third (the reply still starts `<b>Opt-out notice for Gobblr</b>`).

- [ ] **Step 3: Implement** in `app/advocate.py`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/advocate.py tests/test_advocate.py
git commit -m "Hand an unsent opt-out to the member as a finished draft"
```

---

### Task 4: Daily limits and a member cap

**Files:**
- Modify: `app/quota.py`, `app/users.py`, `app/agent.py` (new `is_onboarded`), `app/bot.py` (`_start`, `_watch`, `_ask`, the file and link branches of `handle`)
- Test: `tests/test_quota.py`, `tests/test_users.py`, `tests/test_bot.py`

**Interfaces:**
- Produces:
  - `quota.NEW_SERVICES_PER_DAY = 5`, `quota.CHECKS_PER_DAY = 15`, `quota.QUESTIONS_PER_DAY = 30`.
  - `quota.allowed(conn, user: User, name: str, limit: int) -> bool`: always `True` for the owner, with nothing counted; otherwise `quota.take(conn, name, user.id, limit)`. The names used are `new_service`, `check` and `question`.
  - `agent.is_onboarded(conn, url: str) -> bool`: `find_service(conn, url)` exists and has at least one snapshot.
  - `users.MAX_MEMBERS = 50` and `users.has_room(conn) -> bool` (fewer than `MAX_MEMBERS` users whose status is `active` or `pending`). `users.join_member` and `users.start_email_only_join` create nobody when there is no room; a member who already exists is returned as today.

Where each applies, and the exact replies:

| Where | Counted when | Reply at the limit |
|---|---|---|
| `_watch`, once the URL is known | `not agent.is_onboarded(conn, url)` | `That's today's limit of 5 new services on this shared bot. Services someone already watches are still instant. The limit resets at midnight UTC.` |
| `handle`: a file, or a message with a link | always | `That's today's limit of 15 documents and links on this shared bot. It resets at midnight UTC.` |
| `_ask`, for a non-empty question | always | `That's today's limit of 30 questions on this shared bot. It resets at midnight UTC.` (the metric outcome stays `ok`) |
| `_start` with the right join code, for someone who is not a member, when there is no room | | `This Fine Print bot is full. Ask the person who runs it for a spot.` |

The numbers in the replies come from the constants. Email intake is not limited by this task.

- [ ] **Step 1: Write the failing tests**

`tests/test_quota.py`:

```python
def test_the_owner_is_never_limited(conn):
    owner = users.claim_owner(conn, 1, "owner-code", "Owner")
    member = users.join_member(conn, "join-code", chat_id=2, name="M")
    assert [quota.allowed(conn, owner, "check", 1) for _ in range(3)] == [True, True, True]
    assert [quota.allowed(conn, member, "check", 1) for _ in range(2)] == [True, False]
```

`tests/test_users.py`:

```python
def test_a_full_bot_takes_no_new_members_but_keeps_the_old(conn, monkeypatch):
    monkeypatch.setattr(users, "MAX_MEMBERS", 2)
    first = users.join_member(conn, "join-code", chat_id=1, name="A")
    assert users.join_member(conn, "join-code", chat_id=2, name="B") and not users.has_room(conn)
    assert users.join_member(conn, "join-code", chat_id=3, name="C") is None
    assert users.join_member(conn, "join-code", chat_id=1, name="A").id == first.id
    assert users.start_email_only_join(conn, "join-code", "late@example.com") is None
```

`tests/test_bot.py` (add `from app import quota` and `from app.outgoing import Outgoing`):

```python
def onboarding(monkeypatch, conn):
    """/watch <name> finds https://<name>.test/terms and 'onboards' it by storing one version."""
    async def find(query, cl=None, client=None):
        return f"https://{query}.test/terms"

    async def subscribe(conn_, cl_, user, name, url, on_stage=None):
        svc = watch(conn, user, name, url)
        conn.execute("INSERT OR IGNORE INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (?, ?, 'live', ?, 't')",
                     (svc.id, db.now(), name))
        return svc, []

    monkeypatch.setattr(bot, "find_terms_url", find)
    monkeypatch.setattr(agent, "subscribe", subscribe)


async def test_new_services_are_limited_but_watched_ones_stay_instant(conn, tg, monkeypatch):
    monkeypatch.setattr(quota, "NEW_SERVICES_PER_DAY", 1)
    onboarding(monkeypatch, conn)
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 3, "/start join-code")
    await say(conn, tg, 2, "/watch alpha")
    await say(conn, tg, 2, "/watch beta")
    assert tg.to(2)[-1] == ("That's today's limit of 1 new services on this shared bot. Services someone already watches "
                            "are still instant. The limit resets at midnight UTC.")
    await say(conn, tg, 3, "/watch alpha")      # already onboarded: not counted
    await say(conn, tg, 3, "/watch gamma")      # so this is member 3's first new one
    assert sorted(s.name for s in agent.services_for(conn, users.by_chat(conn, 3).id)) == ["Alpha", "Gamma"]
    for name in ("delta", "epsilon"):            # the owner is not limited
        await say(conn, tg, 1, f"/watch {name}")
    assert len(agent.services_for(conn, users.owner(conn).id)) == 2


async def test_checks_and_questions_are_limited_per_day(conn, tg, monkeypatch):
    monkeypatch.setattr(quota, "CHECKS_PER_DAY", 1)
    monkeypatch.setattr(quota, "QUESTIONS_PER_DAY", 1)
    checked, asked = [], []

    async def check_url(cl, url, on_stage=None, *, conn=None, user=None):
        checked.append(url)
        return Outgoing(text="card")

    async def answer(conn_, cl_, user, question, on_stage=None):
        asked.append(question)
        return SimpleNamespace(outcome="answered", html="answer", service=None)

    monkeypatch.setattr(agent, "check_url", check_url)
    monkeypatch.setattr(bot.ask, "answer", answer)
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 2, "/start join-code")
    for _ in range(2):
        await say(conn, tg, 2, "https://acme.test/terms")
    assert len(checked) == 1 and tg.to(2)[-1] == "That's today's limit of 1 documents and links on this shared bot. It resets at midnight UTC."
    for _ in range(2):
        await say(conn, tg, 2, "/ask can they raise my price?")
    assert len(asked) == 1 and tg.to(2)[-1] == "That's today's limit of 1 questions on this shared bot. It resets at midnight UTC."


async def test_a_full_bot_says_so_to_a_newcomer(conn, tg, monkeypatch):
    monkeypatch.setattr(users, "MAX_MEMBERS", 2)
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 2, "/start join-code")
    await say(conn, tg, 3, "/start join-code")
    assert tg.to(3) == ["This Fine Print bot is full. Ask the person who runs it for a spot."] and users.by_chat(conn, 3) is None
```

Add `from types import SimpleNamespace` to `tests/test_bot.py` if absent.

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_quota.py -k owner tests/test_users.py -k full_bot tests/test_bot.py -k "limited or full_bot"`
Expected: FAIL (`AttributeError: module 'app.quota' has no attribute 'allowed'`).

- [ ] **Step 3: Implement** as specified. Check a limit before any provider work starts, and before the progress bubble opens.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/quota.py app/users.py app/agent.py app/bot.py tests/test_quota.py tests/test_users.py tests/test_bot.py
git commit -m "Limit what each member can spend in a day and cap membership"
```

---

### Task 5: A real alert for a newcomer

**Files:**
- Modify: `app/agent.py` (new `showcase_change`, `watch_existing`), `app/bot.py` (`HELP_BUTTONS` becomes `help_buttons()`, `WELCOME`, `_dispatch`)
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `settings.showcase_service`, button data `h:demo` (Lane 0), `agent.report_for`, `agent.alert`, `notify.send_outgoing`, `advocate.OPT_OUT_THRESHOLD`.
- Produces:
  - `agent.showcase_change(conn) -> tuple[Service, Change] | None`: the service whose name equals `settings.showcase_service` ignoring case, and its newest change (by `effective_at`, then `id`) with `material >= settings.material_threshold` and `opt_out >= advocate.OPT_OUT_THRESHOLD`; `None` when the setting is empty, the service is unknown, or it has no such change.
  - `agent.watch_existing(conn, user: User, svc: Service) -> None`: adds the subscription if it is not there; no fetching and no judging.
  - `bot.help_buttons() -> Buttons`: today's `HELP_BUTTONS` rows, preceded by `[("🎬 See a real alert", "v1:h:demo")]` when `settings.showcase_service` is set. Every place that sends `HELP_BUTTONS` uses it.

Exact copy:

- Before the card: `This is <b>{name}</b>, a fictional company we built to show Fine Print at work. Every button below is real. The opt-out comes back as a draft for you to send, because I only send to an address on a service's own verified domain.`
- When there is nothing to show: `The demo alert isn't ready on this bot yet.`
- `WELCOME` gains, after its first paragraph:

```
Three things to try:
• Tap <b>See a real alert</b> to watch me catch a change and draft the way out.
• Tell me what you use, for example <i>/watch spotify</i>.
• Send me a contract, a link or a screenshot before you agree to it.
```

  The first bullet is included only when `settings.showcase_service` is set (then the heading reads "Three things to try:", otherwise "Two things to try:").

- [ ] **Step 1: Write the failing tests** in `tests/test_bot.py` (add `from app.config import Settings`)

```python
CL = SimpleNamespace(pinecone=None, ai=None, swarm=None)


def showcase(monkeypatch, name="Gobblr"):
    custom = Settings(_env_file=None, showcase_service=name)
    monkeypatch.setattr(bot, "settings", custom)
    monkeypatch.setattr(agent, "settings", custom)


def demo_company(conn):
    svc = agent.add_service(conn, "Gobblr", "https://gobblr.netlify.app/terms")
    conn.execute("INSERT INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (?, ?, 'live', 'a', 't')", (svc.id, db.now()))
    snap = conn.execute("SELECT id FROM snapshots").fetchone()["id"]
    for heading, opt_out, at in (("4. Dispute resolution", 0.9, "2026-11-28T08:00:00+00:00"),
                                 ("4. Dispute resolution (removed)", 0.1, "2026-11-28T09:00:00+00:00")):
        conn.execute(
            "INSERT INTO changes (service_id, from_snap, to_snap, detected_at, effective_at, heading, kind, before_text, after_text,"
            " material, category, opt_out, severity, favours, summary) VALUES (?, ?, ?, ?, ?, ?, 'modified', 'x', 'y', 0.9, 'arbitration', ?, 2.7, 'company', 's')",
            (svc.id, snap, snap, at, at, heading, opt_out))
    return svc


async def test_the_showcase_button_appears_only_when_a_service_is_named(conn, tg, monkeypatch):
    assert bot.help_buttons()[0][0][0] != "🎬 See a real alert"
    showcase(monkeypatch)
    assert bot.help_buttons()[0] == [("🎬 See a real alert", "v1:h:demo")]
    await say(conn, tg, 1, "/start owner-code")
    await say(conn, tg, 2, "/start join-code")
    assert "Three things to try:" in tg.to(2)[0] and tg.last(2)["buttons"][0] == [("🎬 See a real alert", "v1:h:demo")]


async def test_see_a_real_alert_watches_the_demo_and_opens_its_newest_opt_out_change(conn, tg, monkeypatch):
    showcase(monkeypatch)
    svc = demo_company(conn)
    await say(conn, tg, 1, "/start owner-code")
    await bot.handle(conn, CL, tg, Incoming(chat_id=1, kind="tap", data="v1:h:demo", callback_id="cb", message_id=5))
    intro, card = tg.messages[-2], tg.messages[-1]
    assert intro["text"].startswith("This is <b>Gobblr</b>, a fictional company")
    assert "<i>4. Dispute resolution</i>" in card["text"] and "(removed)" not in card["text"]
    assert "✉️ Review & Draft Opt-Out" in [label for row in card["buttons"] for label, _ in row]
    assert [s.id for s in agent.services_for(conn, users.owner(conn).id)] == [svc.id]


async def test_see_a_real_alert_before_the_demo_exists(conn, tg, monkeypatch):
    showcase(monkeypatch)
    await say(conn, tg, 1, "/start owner-code")
    await bot.handle(conn, CL, tg, Incoming(chat_id=1, kind="tap", data="v1:h:demo", callback_id="cb", message_id=5))
    assert tg.to(1)[-1] == "The demo alert isn't ready on this bot yet."
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_bot.py -k "showcase or real_alert"`
Expected: FAIL (`AttributeError: module 'app.bot' has no attribute 'help_buttons'`).

- [ ] **Step 3: Implement.** The `h:demo` arm answers the callback, then either sends the "isn't ready" line or calls `watch_existing`, sends the intro, and sends `agent.alert(await agent.report_for(conn, cl, change, svc, user, {}, {}), svc)` with `notify.send_outgoing`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/agent.py app/bot.py tests/test_bot.py
git commit -m "Show a newcomer a real alert on the demo company"
```

---

### Task 6: The demo company

**Files:**
- Create: `demo/site/v1/index.html`, `demo/site/v1/terms.html`, `demo/site/v2/index.html`, `demo/site/v2/terms.html`, `demo/switch.sh` (executable), `demo/README.md`, `tests/test_demo_site.py`

**Interfaces:**
- Consumes: `watch.html_to_markdown`, `watch.normalise`, `watch.diff`, `watch.sha`, `watch.opt_out_days`, `advocate.dispatchable`.
- Produces: a static site for the fictional service **Gobblr**; `demo/switch.sh v1|v2 [--dry-run]`.

What the pages must be:

- Plain HTML with inline CSS and no external files (a Scout crawl is locked to the site's own host). Every page has the footer `Gobblr is a fictional service made to demonstrate Fine Print. Nothing here is a real offer.`
- `terms.html` uses `<h2>` for these headings, the same in both versions: `1. Your account`, `2. Subscription and fees`, `3. Your content`, `4. Dispute resolution`, `5. Changes to these terms`, `6. Contact`. Each section has at least two sentences. Before the first heading there is one sentence: `This is revision {{REVISION}} of these terms.` The placeholder appears exactly once per file.
- **v1** is mild: a monthly subscription you can cancel any time at the same price; you keep your content and Gobblr only stores and shows it to you; disputes go to the courts where you live.
- **v2** changes exactly three sections:
  - `2. Subscription and fees`: the price may rise at each renewal on 7 days' notice.
  - `3. Your content`: a licence to use, reproduce and adapt what you upload to train and improve Gobblr's artificial intelligence models.
  - `4. Dispute resolution`: binding individual arbitration and a class-action waiver, and this sentence word for word: `You may opt out of this arbitration agreement by emailing optout@gobblr.example within 30 days of the date these terms were last updated, stating your full name and the email address on your account.`
- The stamp must not read as a change: do not write "Last updated" on its own line (the normaliser drops such lines, so the digest would not change), and keep the revision a single token such as `20261128T081400Z`.

`demo/switch.sh v1|v2 [--dry-run]`: copies `demo/site/<version>` to a new temporary directory, replaces `{{REVISION}}` in its `terms.html` with `$(date -u +%Y%m%dT%H%M%SZ)`, then runs `${DEMO_DEPLOY:-netlify deploy --prod --dir}` with that directory as the last argument. With `--dry-run` it prints the directory and deploys nothing. Any other first argument prints the usage line and exits 2.

`demo/README.md` covers: creating a free static site under a subdomain you choose (Netlify, or Cloudflare Pages with `DEMO_DEPLOY="wrangler pages deploy --project-name gobblr"`); why not a project page under a personal GitHub account (the service would be named after the account); the first watch (`demo/switch.sh v1`, then `/watch https://<subdomain>.netlify.app/terms`); `FP_SHOWCASE_SERVICE=Gobblr`; one take (`switch.sh v1`, `/check gobblr`, `switch.sh v2`, `/check gobblr`); and the rules for takes: rehearse and record before the join link is shared and leave the site alone afterwards, pause the daily workflow while recording (it runs at 7:47 PM IST), and the redline kit runs five times a UTC day.

- [ ] **Step 1: Write the failing tests** in `tests/test_demo_site.py`

```python
import re
import subprocess
from pathlib import Path

from app import advocate
from app.advocate import OptOut
from app.watch import diff, html_to_markdown, normalise, opt_out_days, sha

ROOT = Path(__file__).parent.parent / "demo"
SENTENCE = ("You may opt out of this arbitration agreement by emailing optout@gobblr.example within 30 days of the date "
            "these terms were last updated, stating your full name and the email address on your account.")


def raw(version):
    return (ROOT / "site" / version / "terms.html").read_text()


def page(version, revision="20261128T081400Z"):
    return normalise(html_to_markdown(raw(version).replace("{{REVISION}}", revision)))


def test_both_versions_are_readable_fictional_terms_pages():
    for version in ("v1", "v2"):
        assert len(page(version)) >= 400 and raw(version).count("{{REVISION}}") == 1
        assert "Gobblr is a fictional service made to demonstrate Fine Print." in raw(version)
        assert not re.search(r"<(link|script|img)\b|url\(", raw(version))  # nothing loaded from elsewhere


def test_v2_changes_three_sections_and_offers_a_30_day_opt_out_by_email():
    changes = {c.heading: c for c in diff(page("v1"), page("v2"))}
    assert set(changes) == {"2. Subscription and fees", "3. Your content", "4. Dispute resolution"}
    disputes = changes["4. Dispute resolution"].after
    assert SENTENCE in " ".join(disputes.split()) and "binding" in disputes and opt_out_days(disputes) == 30
    assert "train" in changes["3. Your content"].after and "renewal" in changes["2. Subscription and fees"].after


def test_a_new_revision_changes_the_digest_but_is_not_a_change():
    first, second = page("v2", "20261128T081400Z"), page("v2", "20261129T101500Z")
    assert sha(first) != sha(second) and diff(first, second) == []


def test_fine_print_drafts_this_opt_out_and_does_not_send_it():
    opt = OptOut(channel="email", target="optout@gobblr.example", deadline_days=30, verbatim_instruction=SENTENCE)
    assert not advocate.dispatchable(opt, None)  # a page on a shared host has no verified domain


def test_switch_stamps_a_copy_and_refuses_other_arguments():
    done = subprocess.run(["bash", str(ROOT / "switch.sh"), "v2", "--dry-run"], capture_output=True, text=True, check=True)
    stamped = (Path(done.stdout.strip().splitlines()[-1]) / "terms.html").read_text()
    assert "{{REVISION}}" not in stamped and re.search(r"revision \d{8}T\d{6}Z of these terms", stamped)
    assert subprocess.run(["bash", str(ROOT / "switch.sh"), "v3"], capture_output=True).returncode == 2
```

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest -q tests/test_demo_site.py`
Expected: FAIL (`FileNotFoundError: … demo/site/v1/terms.html`).

- [ ] **Step 3: Write** the four pages, the script (`chmod +x`) and the README.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add demo tests/test_demo_site.py
git commit -m "Add the demo company and a script that stages a take"
```

---

### Task 7: The video script

**Files:**
- Create: `docs/demo/script.md`, `tests/test_docs.py`

**Interfaces:**
- Produces: `docs/demo/script.md`, and `tests/test_docs.py` with a helper `unbacked(text: str) -> list[str]` that returns every match of `300\s?ms`, `HMAC` or `fork(s|ed|ing)? (two|sibling|a )` (ignoring case).

What the script contains, for a 2:30 video:

1. **Staging checklist:** the demo site at v1 and watched; `FP_SHOWCASE_SERVICE` set; the census run and spot-checked; the services named in shot two already watched from a second account, so they arrive instantly; a sample lease with a clause that is void under the chosen `/jurisdiction`; the daily workflow paused; at most five redline kits a UTC day; screen recording of the Telegram desktop app at a readable size.
2. **Eight shots** at these marks, each with what to type or tap, what appears, the line to say, and where to cut a wait:

| Time | Shot |
|---|---|
| 0:00 | Hook and the Wrapped card |
| 0:15 | "I use Spotify, Netflix, Uber and Notion": all watched, the year summed up |
| 0:35 | `demo/switch.sh v2`, `/check gobblr`; the Sprites narrate; the alert with its 📊 line |
| 0:55 | The diff, the evidence screenshot, how it was judged (before the next tap, which removes these buttons) |
| 1:10 | Review & Draft Opt-Out: the notice, the clause quoted, the deadline in the calendar; you send it yourself |
| 1:30 | A lease: the verdict, a clause void where you live, the redline kit |
| 1:55 | How it is built: three Sprites, connectors with least privilege, Jev's typed answers |
| 2:15 | `/radar`, the card again |

3. **Honest numbers to say:** a worker's rewind takes about 15 seconds warm and 48 cold; the redline kit takes a few minutes and is cut; Fine Print drafts the demo's opt-out and does not send it.
4. **The two recordings:** a safe take on Sunday night with whatever is live, and the final take on Monday by 11 AM IST.
5. **Optional hook** for the post and the first line: "Everyone has an uncle who reads the terms and conditions. This one lives on a Sprite."

- [ ] **Step 1: Write the failing test** in `tests/test_docs.py`

```python
import re
from pathlib import Path

REPO = Path(__file__).parent.parent
CLAIMS = re.compile(r"300\s?ms|HMAC|fork(s|ed|ing)? (two|sibling|a )", re.IGNORECASE)


def unbacked(text: str) -> list[str]:
    return [m.group(0) for m in CLAIMS.finditer(text)]


def test_the_script_has_every_shot_and_claims_nothing_the_code_does_not_do():
    script = (REPO / "docs" / "demo" / "script.md").read_text()
    for mark in ("0:00", "0:15", "0:35", "0:55", "1:10", "1:30", "1:55", "2:15"):
        assert mark in script
    assert "safe take" in script.lower() and "/check gobblr" in script and unbacked(script) == []
```

- [ ] **Step 2: Run and see it fail**

Run: `uv run pytest -q tests/test_docs.py`
Expected: FAIL (`FileNotFoundError: … docs/demo/script.md`).

- [ ] **Step 3: Write `docs/demo/script.md`.** Plain sentences a person can read aloud; every tap named by the label on its button.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add docs/demo/script.md tests/test_docs.py
git commit -m "Write the demo video script"
```

---

### Task 8: README, licence and submission notes

Do this last, after rebasing on `main`, so the README describes what was actually merged.

**Files:**
- Modify: `README.md`
- Create: `LICENSE`, `docs/demo/submission.md`
- Test: `tests/test_docs.py`

**Interfaces:**
- Consumes: `unbacked` (Task 7).

What each contains:

- **`README.md`**, from the top: the pitch in two lines; a line for the demo video (`Demo video: to be added`, until the owner supplies the link); `## For judges` with what to try (see a real alert, tell it what you use, send it a contract) and the sentence `The join link is in our submission notes.`; `## Who does what`, a table of each provider and its one job, listing only what is merged (TypeSafe's Jev, served by OpenCode, for every judgment; Pinecone for clause memory and the census benchmarks; AgentMail; Exa; LlamaParse; OpenCode's chat models for writing); `## How it is built`, a Mermaid diagram of the Gateway, Scout and Advocate Sprites, the connectors each may use, and the per-job lock, run, rewind cycle. The command table gains every command that was merged (`/check`, `/census`, `/radar`). The existing setup reference stays below, unchanged in substance. Correct any sentence the code does not back.
- **`LICENSE`:** the MIT License, `Copyright (c) 2026 Dhananjay Pandit`.
- **`docs/demo/submission.md`:** the agent's name; two sentences on what it does; the connectors answer, as a list to trim to what is live on the Sprite at submission; notes for judges with a `<join link>` placeholder; a short post (at most 280 characters, tagging @flydotio) and a longer one, neither containing the join link.

- [ ] **Step 1: Write the failing test** in `tests/test_docs.py`

```python
def test_the_readme_licence_and_submission_notes_are_in_place():
    readme = (REPO / "README.md").read_text()
    assert "## For judges" in readme and "The join link is in our submission notes." in readme
    assert "```mermaid" in readme and "t.me/" not in readme.split("## For judges")[1].split("\n## ")[0]
    assert (REPO / "LICENSE").read_text().startswith("MIT License") and "Dhananjay Pandit" in (REPO / "LICENSE").read_text()
    notes = (REPO / "docs" / "demo" / "submission.md").read_text()
    assert "@flydotio" in notes and "<join link>" in notes
    assert unbacked(readme) == [] and unbacked(notes) == []
```

- [ ] **Step 2: Run and see it fail**

Run: `uv run pytest -q tests/test_docs.py::test_the_readme_licence_and_submission_notes_are_in_place`
Expected: FAIL (`assert '## For judges' in readme`).

- [ ] **Step 3: Write** the three documents.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uvx ruff@0.17.6 check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add README.md LICENSE docs/demo/submission.md tests/test_docs.py
git commit -m "Lead the README with the pitch, and add the licence and submission notes"
```

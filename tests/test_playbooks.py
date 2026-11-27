"""Domain playbooks on verdict cards: routing, domain batteries, statutory notes and private conflict checks."""

import json

import httpx
import pytest
import respx

from app import agent, db, playbooks, users

JEV = "https://opencode.test/inference/systemone/v1/systemone"
CHAT = "https://opencode.test/inference/openai/v1/chat/completions"
EMBED = "https://pinecone.test/embed"
QUERY = "https://pinecone.test/indexes/fine-print/query"
UPSERT = "https://pinecone.test/indexes/fine-print/vectors/upsert"

LEASE = """# Residential Lease Agreement
## Repairs
Tenant shall pay for all repairs to appliances, plumbing and heating, including those caused by ordinary wear.
## Entry
Landlord may enter the premises at any time without notice to the tenant for any reason.
"""
OFFER = """# Offer Letter and Proprietary Information and Inventions Agreement
## Inventions
Employee assigns to the Company all inventions conceived at any time during employment, including on personal time.
## Non-competition
For two years after employment ends, Employee shall not work for any competitor of the Company anywhere.
## Confidentiality
Employee shall keep all Company confidential information secret and shall not disclose it to any third-party
service, including artificial intelligence tools, and shall not allow such information to be used to train models.
"""
SOW = """# Master Services Agreement and Statement of Work
## Indemnification
Contractor shall indemnify Client against any and all claims, losses and costs without limitation of amount.
"""
QUOTES = {
    "repair_cost_shifting": "Tenant shall pay for all repairs to appliances, plumbing and heating",
    "landlord_unannounced_entry": "Landlord may enter the premises at any time without notice to the tenant",
    "non_compete_clause": "For two years after employment ends, Employee shall not work for any competitor",
    "side_project_ip_capture": "Employee assigns to the Company all inventions conceived at any time during employment",
    "uncapped_indemnity": "Contractor shall indemnify Client against any and all claims, losses and costs",
}
NOTION_TERMS = """## 1. Using the service
You may use the service to write notes.

## 7. AI features and your content
You grant us a worldwide licence to use your content and inputs to train and improve our AI models.

## 9. Contact
Write to us at the address below.
"""


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "p.db")


@pytest.fixture
def members(conn):
    return [users.join_member(conn, "join-code", chat_id=i, name=n) for i, n in ((1, "A"), (2, "B"))]


@pytest.fixture(autouse=True)
def no_grade(monkeypatch):
    async def none(q, client=None):
        return None

    monkeypatch.setattr(agent.tosdr, "lookup", none)


class Jev:
    """Answers each System One request by its kind: router, red flags, statutes, conflicts or severity."""

    def __init__(self, domain="consumer_tos", found=(), statute=0.95, conflict=0.9, severity=2.8, router_fails=False):
        self.domain, self.found, self.statute, self.conflict = domain, set(found), statute, conflict
        self.severity, self.router_fails = severity, router_fails
        self.bodies: list[dict] = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        out = {}
        for key, q in body["questions"].items():
            if q["type"] == "choice":
                if self.router_fails:
                    return httpx.Response(400, json={"error": "bad"})
                out[key] = {"type": "choice", "choice": self.domain, "confidence": 0.9, "probabilities": {self.domain: 0.9}}
            elif q["type"] == "score":
                out[key] = {"type": "score", "score": self.severity, "confidence": 0.8,
                            "probabilities": {"0": 0.0, "1": 0.0, "2": 0.2, "3": 0.8}}
            elif key.startswith("k") and key[1:].isdigit():
                out[key] = {"type": "noul", "noul": self.conflict}
            elif "jurisdiction" in body["state"]:
                out[key] = {"type": "noul", "noul": self.statute}
            else:
                out[key] = {"type": "noul", "noul": 0.9 if key in self.found else 0.05}
        return httpx.Response(200, json={"model": "jev-1.14", "answers": out})

    def battery(self) -> set[str]:
        """The flag keys asked in the red-flag request."""
        return next(set(b["questions"]) for b in self.bodies if "document" in b["state"] and "document_title" in b["state"]
                    and next(iter(b["questions"].values()))["type"] == "noul")


def mock(jev: Jev, found=()):
    reply = "PUSH BACK\n- worth a look\n" + "\n".join(f"QUOTE {k}: {QUOTES[k]}" for k in found if k in QUOTES)
    routes = {
        "jev": respx.post(JEV).mock(side_effect=jev),
        "chat": respx.post(CHAT).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})),
        "embed": respx.post(EMBED).mock(return_value=httpx.Response(200, json={"data": [{"values": [0.1] * 1024}]})),
        "query": respx.post(QUERY).mock(return_value=httpx.Response(200, json={"matches": []})),
        "upsert": respx.post(UPSERT).mock(return_value=httpx.Response(200, json={"upsertedCount": 1})),
    }
    return routes


async def card_for(title, text, **kw):
    cl = agent.Clients()
    try:
        return await agent.verdict_card(cl, title, text, **kw)
    finally:
        await cl.aclose()


# --- Routing and domain batteries ----------------------------------------------------------------

@respx.mock
async def test_lease_runs_the_lease_battery_with_lease_explain_buttons():
    found = ("repair_cost_shifting", "landlord_unannounced_entry")
    jev = Jev("residential_lease", found)
    mock(jev, found)
    card = await card_for("Lease", LEASE)
    assert "📂 <i>Residential lease</i>" in card.text
    assert jev.battery() == set(playbooks.battery("residential_lease"))
    assert "🔴 Found (90% sure): repair cost shifting" in card.text
    assert "security deposit trap" in card.text  # deposit traps are checked too
    assert "<b>repair cost shifting</b>: “Tenant shall pay" in card.text  # verified verbatim quote
    assert ("💡 repair cost shifting", "v1:fx:repair_cost_shifting") in [b for row in card.buttons for b in row]
    assert card.check == playbooks.ContractCheck(domain="residential_lease", found=list(found))
    assert card.check.offers_redline


@respx.mock
async def test_employment_offer_runs_the_employment_battery():
    found = ("side_project_ip_capture", "non_compete_clause")
    jev = Jev("employment_or_nda", found)
    mock(jev, found)
    card = await card_for("Offer", OFFER)
    assert "Employment / NDA" in card.text and jev.battery() == set(playbooks.battery("employment_or_nda"))
    assert "🔴 Found (90% sure): side project ip capture" in card.text
    assert "🔴 Found (90% sure): non compete clause" in card.text
    assert card.check.domain == "employment_or_nda" and card.check.found == list(found)


@respx.mock
async def test_freelance_contract_runs_the_freelance_battery():
    jev = Jev("freelance_or_saas", ("uncapped_indemnity",))
    mock(jev, ("uncapped_indemnity",))
    card = await card_for("MSA", SOW)
    assert "Freelance / SaaS contract" in card.text and jev.battery() == set(playbooks.battery("freelance_or_saas"))
    assert "🔴 Found (90% sure): uncapped indemnity" in card.text
    assert [b[1] for row in card.buttons for b in row] == ["v1:fx:uncapped_indemnity"]


@respx.mock
async def test_router_failure_falls_back_to_consumer_terms():
    jev = Jev(router_fails=True, found=("arbitration",))
    mock(jev)
    card = await card_for("Terms", "Some terms of service.")
    assert jev.battery() == set(playbooks.battery("consumer_tos"))
    assert "Consumer terms" in card.text and card.check.domain == "consumer_tos"
    assert not card.check.offers_redline  # consumer terms aren't a personal contract


# --- Statutory notes ----------------------------------------------------------------------------

def _set(conn, user, code):
    users.set_jurisdiction(conn, user, code)
    return users.by_id(conn, user.id)


@respx.mock
async def test_california_member_gets_statutory_notes_with_disclaimer(conn, members):
    a = _set(conn, members[0], "US-CA")
    found = ("side_project_ip_capture", "non_compete_clause")
    jev = Jev("employment_or_nda", found)
    mock(jev, found)
    card = await card_for("Offer", OFFER, conn=conn, user=a)
    assert "<b>Where you live (California, USA)</b>" in card.text
    assert "may be void under California law" in card.text and "Cal. Bus. &amp; Prof. Code § 16600" in card.text
    assert "Cal. Labor Code § 2870" in card.text
    assert "Information, not legal advice." in card.text
    [statute] = [b for b in jev.bodies if "jurisdiction" in b["state"]]
    assert set(statute["questions"]) == set(found)  # one Jev batch, one question per mapped flag
    assert QUOTES["non_compete_clause"] in statute["state"].values()  # judged against the verified clause


@respx.mock
async def test_eu_member_gets_gdpr_note_for_ai_training(conn, members):
    a = _set(conn, members[0], "EU")
    jev = Jev("privacy_and_ai", ("ai_model_training",))
    mock(jev)
    card = await card_for("Privacy", "We use your content to train our AI models.", conn=conn, user=a)
    assert "GDPR Art. 6 and Art. 21" in card.text and "may be restricted under EU data protection law" in card.text
    assert "not legal advice" in card.text


@respx.mock
async def test_statutory_note_is_omitted_when_jev_does_not_confirm(conn, members):
    a = _set(conn, members[0], "US-CA")
    jev = Jev("employment_or_nda", ("non_compete_clause",), statute=0.2)
    mock(jev, ("non_compete_clause",))
    card = await card_for("Offer", OFFER, conn=conn, user=a)
    assert "Where you live" not in card.text and "16600" not in card.text


@respx.mock
async def test_member_without_jurisdiction_gets_a_pointer_and_no_notes(conn, members):
    jev = Jev("employment_or_nda", ("non_compete_clause",))
    mock(jev, ("non_compete_clause",))
    card = await card_for("Offer", OFFER, conn=conn, user=members[0])
    assert agent.JURISDICTION_HINT in card.text and "Where you live" not in card.text
    assert not any("jurisdiction" in b["state"] for b in jev.bodies)


@respx.mock
async def test_indian_state_member_gets_india_wide_notes(conn, members):
    a = _set(conn, members[0], "IN-KA")
    found = ("non_compete_clause",)
    jev = Jev("employment_or_nda", found)
    mock(jev, found)
    card = await card_for("Offer", OFFER, conn=conn, user=a)
    assert "<b>Where you live (Karnataka, India)</b>" in card.text
    assert "Indian Contract Act, 1872, s. 27" in card.text and "may be void under Indian law" in card.text
    assert "Information, not legal advice." in card.text


def test_india_rules_apply_to_every_state_and_union_territory():
    assert len(playbooks.INDIA_STATES) == 36 and all(c.startswith("IN-") for c in playbooks.INDIA_STATES)
    for code in ("IN", *playbooks.INDIA_STATES):
        assert code in playbooks.JURISDICTIONS
        rules = playbooks.statutes_for(code, "privacy_and_ai", ["ai_model_training", "no_deletion_right"])
        assert {r.law for r in rules.values()} == {
            "Digital Personal Data Protection Act, 2023, s. 6", "Digital Personal Data Protection Act, 2023, s. 12"}
    assert playbooks.statutes_for("US-CA", "privacy_and_ai", ["ai_model_training"]) == {}  # no fallback elsewhere
    for flag in ("non_compete_clause",):
        assert playbooks.statutes_for("IN-MH", "employment_or_nda", [flag])[flag].law == "Indian Contract Act, 1872, s. 27"


def test_set_jurisdiction_accepts_indian_codes_and_old_aliases(conn, members):
    a = members[0]
    assert users.set_jurisdiction(conn, a, "in") == "IN"
    assert users.set_jurisdiction(conn, a, "in-mh") == "IN-MH"
    assert users.set_jurisdiction(conn, a, "IN-TG") == "IN-TS"  # Telangana's pre-2023 ISO code
    assert users.set_jurisdiction(conn, a, "IN-UT") == "IN-UK"  # Uttarakhand's
    with pytest.raises(ValueError):
        users.set_jurisdiction(conn, a, "IN-XX")


def test_set_jurisdiction_validates_and_clears(conn, members):
    a = members[0]
    assert users.set_jurisdiction(conn, a, "us-ca") == "US-CA"
    assert users.by_id(conn, a.id).jurisdiction == "US-CA"
    assert users.by_id(conn, members[1].id).jurisdiction is None  # that member only
    with pytest.raises(ValueError):
        users.set_jurisdiction(conn, a, "MARS")
    assert users.by_id(conn, a.id).jurisdiction == "US-CA"
    assert users.set_jurisdiction(conn, a, "clear") is None
    assert users.by_id(conn, a.id).jurisdiction is None


# --- Ephemeral cross-contract conflicts ------------------------------------------------------------

def _watch_notion(conn, *watchers):
    svc = agent.add_service(conn, "Notion", "https://notion.test/terms")
    conn.execute("INSERT INTO snapshots (service_id, taken_at, origin, sha, text) VALUES (?, ?, ?, ?, ?)",
                 (svc.id, db.now(), "live", "sha-n", NOTION_TERMS))
    for user in watchers:
        conn.execute("INSERT INTO subscriptions (user_id, service_id, created_at) VALUES (?, ?, ?)",
                     (user.id, svc.id, db.now()))
    return svc


def _dump(conn) -> str:
    out = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        out += [" ".join(str(v) for v in tuple(row)) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
    return "\n".join(out)


@respx.mock
async def test_nda_conflict_with_watched_service_is_private_and_nothing_is_stored(conn, members):
    a, b = members
    _watch_notion(conn, a, b)
    marker = "ZEBRA-NDA-7731"
    nda = OFFER + f"\n## Reference\n{marker} confidential reference number.\n"
    jev = Jev("employment_or_nda", ("non_compete_clause",), conflict=0.9)
    routes = mock(jev, ("non_compete_clause",))
    before = _dump(conn)

    card = await card_for("NDA", nda, conn=conn, user=a)

    assert "<b>Conflicts with services you use</b>" in card.text
    assert "<b>Notion</b> · <i>7. AI features and your content</i> may conflict" in card.text
    assert "<i>Confidentiality</i>" in card.text  # names the contract clause
    [batch] = [x for x in jev.bodies if any(k.startswith("k") for k in x["questions"])]
    assert len(batch["questions"]) <= playbooks.MAX_CONFLICT_CANDIDATES
    assert any("AI features" in v for v in batch["state"].values())
    assert "1. Using the service" not in json.dumps(batch["state"])  # only clauses about AI, licences or data
    # Nothing was written anywhere: no table changed, the contract text is in no row, nothing was upserted.
    assert _dump(conn) == before and marker not in _dump(conn)
    assert not routes["upsert"].called
    assert all(marker not in c.request.content.decode() for c in routes["query"].calls)
    # No notification for B: nothing was queued, and B's own card (same document) only shows B's watchlist.


@respx.mock
async def test_conflict_check_only_uses_the_uploaders_own_watchlist(conn, members):
    a, b = members
    _watch_notion(conn, b)  # only B watches Notion
    jev = Jev("employment_or_nda", ())
    mock(jev)
    card = await card_for("NDA", OFFER, conn=conn, user=a)
    assert "Conflicts" not in card.text and "Notion" not in card.text
    assert not any(any(k.startswith("k") for k in x["questions"]) for x in jev.bodies)  # no conflict batch at all


@respx.mock
async def test_unconfirmed_conflicts_and_consumer_documents_are_not_reported(conn, members):
    a = members[0]
    _watch_notion(conn, a)
    jev = Jev("employment_or_nda", (), conflict=0.5)
    mock(jev)
    assert "Conflicts" not in (await card_for("NDA", OFFER, conn=conn, user=a)).text
    jev2 = Jev("consumer_tos", ())
    respx.reset()
    mock(jev2)
    await card_for("Terms", OFFER, conn=conn, user=a)
    assert not any(any(k.startswith("k") for k in x["questions"]) for x in jev2.bodies)


# --- Benchmarks on verdict cards are query-only ---------------------------------------------------

@respx.mock
async def test_private_document_benchmark_queries_but_never_upserts():
    jev = Jev("freelance_or_saas", ("uncapped_indemnity",))
    routes = mock(jev, ("uncapped_indemnity",))
    matches = [{"id": f"chg-{i}", "score": 0.9 - i / 100,
                "metadata": {"service": f"Svc{i}", "category": "liability", "severity": i / 6, "public": True,
                             "has_opt_out": i % 2 == 0}} for i in range(12)]
    routes["query"].mock(return_value=httpx.Response(200, json={"matches": matches}))
    card = await card_for("MSA", SOW)
    assert "📊 <b>uncapped indemnity</b>: More severe than" in card.text and "across 12 public services" in card.text
    assert routes["query"].called and not routes["upsert"].called
    body = json.loads(routes["query"].calls.last.request.content)
    assert body["filter"] == {"public": True, "category": "liability"} and body["topK"] == 50

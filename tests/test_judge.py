import httpx
import pytest
import respx
from pydantic import ValidationError

from app.judge import QUESTIONS, SystemOneResponse, parse_verdict, severity_label

GOOD = {
    "model": "jev-1.14.0",
    "answers": {
        "material": {"type": "noul", "noul": 0.91},
        "category": {"type": "choice", "choice": "arbitration", "confidence": 0.8,
                     "probabilities": {"arbitration": 0.85, "other": 0.15}},
        "opt_out": {"type": "noul", "noul": 0.88},
        "favours": {"type": "choice", "choice": "company", "confidence": 0.9,
                    "probabilities": {"company": 0.95, "consumer": 0.02, "neutral": 0.03}},
        "severity": {"type": "score", "score": 2.6, "confidence": 0.6,
                     "probabilities": {"0": 0.0, "1": 0.1, "2": 0.2, "3": 0.7}, "legend": {}},
    },
    "usage": {"input_tokens": 900, "output_tokens": 12},
}


def test_parse_verdict():
    v = parse_verdict(GOOD)
    assert v.is_material() and v.needs_action
    assert (v.category, v.favours, v.severity) == ("arbitration", "company", 2.6)


def test_unknown_category_is_rejected():
    bad = {**GOOD, "answers": {**GOOD["answers"], "category": {**GOOD["answers"]["category"], "choice": "weather"}}}
    with pytest.raises(ValidationError):
        parse_verdict(bad)


def test_probability_out_of_range_is_rejected():
    bad = {**GOOD, "answers": {**GOOD["answers"], "material": {"type": "noul", "noul": 1.4}}}
    with pytest.raises(ValidationError):
        SystemOneResponse.model_validate(bad)


def test_wrong_answer_type_for_question_fails_loudly():
    bad = {**GOOD, "answers": {**GOOD["answers"], "material": GOOD["answers"]["category"]}}
    with pytest.raises(AssertionError):
        parse_verdict(bad)


def test_questions_respect_api_limits():
    for q in QUESTIONS.values():
        if q["type"] == "score":
            assert 2 <= len(q["criteria"]) <= 10
        if q["type"] == "choice":
            assert 1 <= len(q["criteria"]) <= 255


def test_change_deep_in_a_long_section_is_shown_to_the_judge():
    from app.judge import MAX_SECTION_CHARS, state_for
    from app.watch import SectionChange

    filler = "Standard boilerplate sentence about the service. " * 1000  # ~50k chars
    before = filler + "Disputes go to the courts of California." + filler
    after = filler + "Disputes go to binding arbitration in Texas." + filler
    state, partial = state_for("X", SectionChange(heading="General", kind="modified", before=before, after=after))
    assert not partial
    assert "binding arbitration in Texas" in state["after"] and "courts of California" in state["before"]
    assert len(state["after"]) <= MAX_SECTION_CHARS + 10 and state["after"].startswith("[…] ")


def test_change_too_large_to_show_fails_closed():
    from app.judge import state_for
    from app.watch import SectionChange

    state, partial = state_for("Snapchat", SectionChange(heading="", kind="added", before="", after="new clause. " * 10_000))
    assert partial and state["before"] == "(this section did not exist)"
    quiet = {**GOOD, "answers": {**GOOD["answers"], "material": {"type": "noul", "noul": 0.02}}}
    assert parse_verdict(quiet, partial=True).is_material()
    assert not parse_verdict(quiet).is_material()


@pytest.mark.parametrize(
    "score,label",
    [(0, "Cosmetic"), (0.5, "Minor"), (1.49, "Minor"), (1.5, "Worth knowing"), (2.5, "Major"), (2.6, "Major"),
     (3, "Major"), (3.4, "Major")],
)
def test_severity_label(score, label):
    assert severity_label(score) == label


# --- relevance of candidate clauses to a member's question ---------------------------------------

RELEVANCE_URL = "https://opencode.test/inference/systemone/v1/systemone"


def _relevance_gateway():
    from app.config import Provider
    from app.gateway import Gateway

    return Gateway(Provider(name="opencode", base_url="https://opencode.test/inference"))


def _noul_response(**scores):
    return {
        "model": "jev-1.14.0",
        "answers": {key: {"type": "noul", "noul": p} for key, p in scores.items()},
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


@respx.mock
async def test_relevance_scores_in_order():
    import json

    from app.judge import relevance

    route = respx.post(RELEVANCE_URL).mock(return_value=httpx.Response(200, json=_noul_response(c1=0.9, c2=0.1)))
    scores = await relevance(_relevance_gateway(), "Can I get a refund?", ["Refunds are issued within 30 days.", "Contact us."])
    assert scores == [0.9, 0.1]
    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert body["state"]["question"] == "Can I get a refund?"
    assert body["state"]["clause_1"] == "Refunds are issued within 30 days."
    assert list(body["questions"]) == ["c1", "c2"]
    assert all(q["type"] == "noul" for q in body["questions"].values())


@respx.mock
async def test_relevance_no_clauses_makes_no_call():
    from app.judge import relevance

    route = respx.post(RELEVANCE_URL).mock(return_value=httpx.Response(200, json=_noul_response()))
    assert await relevance(_relevance_gateway(), "Anything?", []) == []
    assert route.call_count == 0


@respx.mock
async def test_relevance_missing_answer_scores_zero():
    from app.judge import relevance

    respx.post(RELEVANCE_URL).mock(return_value=httpx.Response(200, json=_noul_response(c1=0.7)))
    assert await relevance(_relevance_gateway(), "Q?", ["a", "b"]) == [0.7, 0.0]

@respx.mock
async def test_relevance_error_body_is_not_logged(caplog):
    import logging

    from app.judge import relevance

    caplog.set_level(logging.DEBUG)
    respx.post(RELEVANCE_URL).mock(
        return_value=httpx.Response(400, json={"error": "state.question: zebra-question is invalid"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await relevance(_relevance_gateway(), "zebra-question?", ["a clause"])
    assert "zebra-question" not in caplog.text
    assert "-> 400" in caplog.text


def test_every_red_flag_has_a_short_explanation():
    from app import playbooks
    from app.judge import FLAG_EXPLANATIONS, RED_FLAGS

    every = [key for pb in playbooks.PLAYBOOKS.values() for key in pb.flags]
    assert len(every) == len(set(every))  # a key belongs to exactly one domain (💡 data carries only the key)
    assert set(FLAG_EXPLANATIONS) == set(every)
    assert all(0 < len(text) <= 200 for text in FLAG_EXPLANATIONS.values())
    assert len(set(FLAG_EXPLANATIONS.values())) == len(FLAG_EXPLANATIONS)  # each explanation is unique
    assert set(RED_FLAGS) <= set(FLAG_EXPLANATIONS)


def test_all_five_domains_have_batteries_and_consumer_flags_are_unchanged():
    from app import playbooks
    from app.judge import RED_FLAGS

    assert set(playbooks.PLAYBOOKS) == {
        "consumer_tos", "privacy_and_ai", "residential_lease", "employment_or_nda", "freelance_or_saas"}
    assert list(RED_FLAGS) == ["auto_renews", "arbitration", "data_sharing", "unilateral_changes",
                               "content_licence", "hard_to_cancel"]
    assert RED_FLAGS["arbitration"] == "Does the document require disputes to go to binding arbitration or waive class actions?"
    lease = playbooks.battery("residential_lease")
    assert {"auto_lease_renewal", "repair_cost_shifting", "early_termination_penalty", "non_refundable_fees",
            "security_deposit_trap", "landlord_unannounced_entry", "habitability_waiver"} == set(lease)
    assert playbooks.battery("nonsense") == RED_FLAGS


def test_every_flag_fits_an_explain_button():
    from app import playbooks, taps

    for key in playbooks.ALL_FLAGS:
        assert taps.parse(taps.build("fx", key)) == taps.Tap("fx", key)


def test_statutory_rules_name_known_flags_and_jurisdictions():
    from app import playbooks

    for (where, domain, flag), rule in playbooks.STATUTES.items():
        assert where in playbooks.JURISDICTIONS and flag in playbooks.battery(domain)
        assert rule.note.startswith("may be ") and "`clause`" in rule.question


def _choice(choice, confidence):
    return {"model": "jev-1.14.0", "answers": {"domain": {
        "type": "choice", "choice": choice, "confidence": confidence, "probabilities": {choice: confidence}}}}


@respx.mock
@pytest.mark.parametrize("choice,confidence,expected", [
    ("residential_lease", 0.9, "residential_lease"),
    ("employment_or_nda", 0.8, "employment_or_nda"),
    ("freelance_or_saas", 0.4, "consumer_tos"),   # unsure: the consumer battery runs
    ("weather_report", 0.99, "consumer_tos"),     # not a domain we know
])
async def test_classify_domain_routes_or_falls_back(choice, confidence, expected):
    import json

    from app.judge import classify_domain

    route = respx.post(RELEVANCE_URL).mock(return_value=httpx.Response(200, json=_choice(choice, confidence)))
    domain, _ = await classify_domain(_relevance_gateway(), "Lease", "x" * 50_000)
    assert domain == expected
    body = json.loads(route.calls.last.request.content)
    assert len(body["state"]["document"]) == 12_000  # header/sample only
    q = body["questions"]["domain"]
    assert q["type"] == "choice" and set(q["criteria"]) >= {"residential_lease", "consumer_tos"}

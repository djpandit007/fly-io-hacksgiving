import pytest
from pydantic import ValidationError

from app.judge import QUESTIONS, SystemOneResponse, parse_verdict

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


def test_huge_sections_are_clipped_to_fit_jev_limits():
    from app.judge import MAX_SECTION_CHARS, state_for
    from app.watch import SectionChange

    state = state_for("Snapchat", SectionChange(heading="", kind="added", before="", after="x " * 60_000))
    assert len(state["after"]) <= MAX_SECTION_CHARS + 20 and state["after"].endswith("[…truncated]")
    assert state["before"] == "(this section did not exist)"

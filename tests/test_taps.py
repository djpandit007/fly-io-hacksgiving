"""Button data grammar: every action round-trips, anything else is refused."""

import pytest

from app import taps
from app.taps import Tap


@pytest.mark.parametrize("action,arg,extra", [
    ("h", "list", ""), ("h", "dl", ""), ("h", "watch", ""), ("h", "how", ""),
    ("w", "notion", ""), ("w", "youtube.com", ""), ("n", "", ""),
    ("k", "", ""), ("u", "42", ""), ("r", "42", ""), ("fx", "arbitration", ""),
    ("fb", "AbC_-123xyz0", "u"), ("fb", "AbC_-123xyz0", "d"),
    ("fr", "AbC_-123xyz0", "w"), ("fr", "AbC_-123xyz0", "c"), ("fr", "AbC_-123xyz0", "n"),
])
def test_every_action_round_trips(action, arg, extra):
    data = taps.build(action, arg, extra)
    assert len(data.encode()) <= 64 and taps.parse(data) == Tap(action, arg, extra)


@pytest.mark.parametrize("data", [
    "", "v1", "v2:h:list", "v1:zz", "v1:h:secrets", "v1:n:extra", "v1:k:x", "v1:u:abc", "v1:u:-1", "v1:u:١٢",
    "v1:u:1234567890123", "v1:fx:not_a_flag", "v1:fb:short:u", "v1:fb:AbC_-123xyz0:x", "v1:fr:AbC_-123xyz0:u",
    "v1:w:has space", "v1:w:-leading", "v1:w:" + "a" * 59, "v1:h:list:x:y", "v1:" + "x" * 70,
])
def test_anything_else_is_refused(data):
    assert taps.parse(data) is None


def test_build_refuses_what_parse_would_refuse():
    with pytest.raises(ValueError):
        taps.build("w", "a" * 59)
    with pytest.raises(ValueError):
        taps.build("u", "notanid")


def test_only_watch_is_slow():
    assert taps.is_slow("v1:w:notion")
    assert not any(taps.is_slow(d) for d in ("v1:h:list", "v1:u:1", "v1:n", "junk"))


@pytest.mark.parametrize("text,expected", [
    ("notion", "notion"), (" youtube.com ", "youtube.com"), ("what does this bot do?", None),
    ("a", None), ("x" * 59, None), ("https://acme.test", None),
    ("thanks.", None), ("ok...", None), ("a..", None), (".com", None), ("notion-", None), ("a.-b", None),
    ("sub.example.co.uk", "sub.example.co.uk"), ("t-mobile.com", "t-mobile.com"), ("x" * 58, "x" * 58),
])
def test_suggestion(text, expected):
    assert taps.suggestion(text) == expected


REF = "A" * 22


@pytest.mark.parametrize("action,arg,extra", [
    ("cd", "7", ""), ("ce", "7", ""), ("ch", "7", ""), ("co", "123456789012", ""), ("cl", "3", ""),
    ("wa", REF, ""), ("rf", "arbitration", ""), ("rf", "ai_model_training", ""), ("h", "demo", ""),
])
def test_new_actions_round_trip(action, arg, extra):
    data = taps.build(action, arg, extra)
    assert len(data.encode()) <= 64 and taps.parse(data) == Tap(action, arg, extra)


@pytest.mark.parametrize("data", [
    "v1:cd:abc", "v1:ce:-1", "v1:ch:1234567890123", "v1:co:7:x", "v1:cl:", "v1:wa:short", f"v1:wa:{REF}:x",
    "v1:rf:not_a_flag", "v1:rf:arbitration:x", "v1:h:secrets",
])
def test_forged_new_actions_are_refused(data):
    assert taps.parse(data) is None


def test_inspector_taps_are_quick_and_watch_many_is_slow():
    assert taps.is_slow(f"v1:wa:{REF}")
    assert not any(taps.is_slow(d) for d in ("v1:cd:7", "v1:ce:7", "v1:ch:7", "v1:co:7", "v1:cl:3", "v1:rf:arbitration", "v1:h:demo"))


def test_every_new_action_has_a_metric_name():
    assert {a: taps.METRIC[a] for a in ("cd", "ce", "ch", "co", "cl", "wa", "rf")} == {
        "cd": "inspect_diff", "ce": "inspect_evidence", "ch": "inspect_how", "co": "open_change",
        "cl": "list_changes", "wa": "watch_many", "rf": "radar_flag"}

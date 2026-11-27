"""Inline-button data: a short versioned grammar, `v1:<action>[:<arg>[:<extra>]]`.

Button data comes back from the client and is untrusted: `parse` accepts only the shapes below, and the
bot still checks at tap time that whatever a tap acts on belongs to the member who tapped.

    v1:h:<list|dl|watch|how>       help buttons            v1:u:<service id>   unwatch
    v1:w:<name>                yes, watch this          v1:r:<service id>   undo an unwatch
    v1:n                       no thanks                v1:fx:<flag key>    explain a red flag
    v1:fb:<token>:<u|d>        rate an answer           v1:fr:<token>:<w|c|n>  reason for a 👎
    v1:k                       an answer already rated (inert)
    v1:oc:<change id>          draft an opt-out from a change alert
    v1:od:<ref>                draft an opt-out offered on a verdict card (RAM-only offer)
    v1:oa:<secret>             approve & send a drafted opt-out (single-use approval token)
    v1:ox:<secret>             cancel a drafted opt-out
    v1:rk:<ref>                redline kit: pick a tone (RAM-only contract)
    v1:rt:<ref>:<p|b|f>        redline kit: generate at that tone
"""

from __future__ import annotations

import re
from typing import NamedTuple

from app.judge import FLAG_EXPLANATIONS
from app.telegram import CALLBACK_DATA_MAX

VERSION = "v1"
HELP_TOPICS = frozenset({"list", "dl", "watch", "how"})
# One token that could be a service name or domain: dot-separated labels of letters, digits and inner
# hyphens (no leading, trailing or doubled dots or hyphens at label edges), 2-58 characters in all.
# 58 keeps "v1:w:<name>" within 64 bytes.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
SERVICE_NAME = re.compile(rf"(?=.{{2,58}}$){_LABEL}(?:\.{_LABEL})*")
TOKEN = re.compile(r"[A-Za-z0-9_-]{12}")
# Approval secrets and RAM cache references: 128 random bits, URL-safe base64 (22 characters).
SECRET = re.compile(r"[A-Za-z0-9_-]{22}")
TONES = {"p": "polite", "b": "balanced", "f": "firm"}
VOTES = {"u": "up", "d": "down"}
REASONS = {"w": "wrong", "c": "unclear", "n": "not_useful"}
# Taps that start provider work; everything else is a quick lookup or a single write.
SLOW = frozenset({"w", "oc", "oa", "rt"})
# Metric names per action (votes and reasons are recorded by answer feedback instead).
METRIC = {"h": "help", "w": "watch", "n": "no", "u": "unwatch", "r": "undo", "fx": "explain",
          "oc": "opt_out_draft", "od": "opt_out_draft", "oa": "opt_out_send", "ox": "opt_out_cancel",
          "rk": "redline", "rt": "redline_kit"}


class Tap(NamedTuple):
    action: str
    arg: str = ""
    extra: str = ""


def _valid(tap: Tap) -> bool:
    match tap.action:
        case "h":
            return tap.arg in HELP_TOPICS and not tap.extra
        case "w":
            return bool(SERVICE_NAME.fullmatch(tap.arg)) and not tap.extra
        case "n" | "k":
            return not tap.arg and not tap.extra
        case "u" | "r":
            return tap.arg.isascii() and tap.arg.isdigit() and len(tap.arg) <= 12 and not tap.extra
        case "fx":
            return tap.arg in FLAG_EXPLANATIONS and not tap.extra
        case "fb":
            return bool(TOKEN.fullmatch(tap.arg)) and tap.extra in VOTES
        case "fr":
            return bool(TOKEN.fullmatch(tap.arg)) and tap.extra in REASONS
        case "oc":
            return tap.arg.isascii() and tap.arg.isdigit() and len(tap.arg) <= 12 and not tap.extra
        case "od" | "oa" | "ox" | "rk":
            return bool(SECRET.fullmatch(tap.arg)) and not tap.extra
        case "rt":
            return bool(SECRET.fullmatch(tap.arg)) and tap.extra in TONES
    return False


def parse(data: str) -> Tap | None:
    if not data or len(data.encode()) > CALLBACK_DATA_MAX:
        return None
    parts = data.split(":")
    if parts[0] != VERSION or not 2 <= len(parts) <= 4:
        return None
    tap = Tap(*parts[1:])
    return tap if _valid(tap) else None


def build(action: str, arg: str = "", extra: str = "") -> str:
    """Button data for a tap; raises ValueError for anything `parse` would refuse."""
    tap = Tap(action, arg, extra)
    data = ":".join(p for p in (VERSION, action, arg, extra) if p)
    if not _valid(tap) or len(data.encode()) > CALLBACK_DATA_MAX:
        raise ValueError(f"invalid button data for action {action!r}")
    return data


def is_slow(data: str) -> bool:
    tap = parse(data)
    return tap is not None and tap.action in SLOW


def suggestion(text: str) -> str | None:
    """The service name in free text that looks like one, if it fits in a button."""
    text = text.strip()
    return text if SERVICE_NAME.fullmatch(text) else None

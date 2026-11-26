"""Inline-button data: a short versioned grammar, `v1:<action>[:<arg>[:<extra>]]`.

Button data comes back from the client and is untrusted: `parse` accepts only the shapes below, and the
bot still checks at tap time that whatever a tap acts on belongs to the member who tapped.

    v1:h:<list|dl|watch|how>   help buttons             v1:u:<service id>   unwatch
    v1:w:<name>                yes, watch this          v1:r:<service id>   undo an unwatch
    v1:n                       no thanks                v1:fx:<flag key>    explain a red flag
    v1:fb:<token>:<u|d>        rate an answer           v1:fr:<token>:<w|c|n>  reason for a 👎
"""

from __future__ import annotations

import re
from typing import NamedTuple

from app.judge import RED_FLAGS
from app.telegram import CALLBACK_DATA_MAX

VERSION = "v1"
HELP_TOPICS = frozenset({"list", "dl", "watch", "how"})
# One token that could be a service name or domain. 58 characters keeps "v1:w:<name>" within 64 bytes.
SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{1,57}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{12}")
VOTES = {"u": "up", "d": "down"}
REASONS = {"w": "wrong", "c": "unclear", "n": "not_useful"}
# Taps that start provider work; everything else is a quick lookup or a single write.
SLOW = frozenset({"w"})
# Metric names per action (votes and reasons are recorded by answer feedback instead).
METRIC = {"h": "help", "w": "watch", "n": "no", "u": "unwatch", "r": "undo", "fx": "explain"}


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
        case "n":
            return not tap.arg and not tap.extra
        case "u" | "r":
            return tap.arg.isascii() and tap.arg.isdigit() and len(tap.arg) <= 12 and not tap.extra
        case "fx":
            return tap.arg in RED_FLAGS and not tap.extra
        case "fb":
            return bool(TOKEN.fullmatch(tap.arg)) and tap.extra in VOTES
        case "fr":
            return bool(TOKEN.fullmatch(tap.arg)) and tap.extra in REASONS
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

"""Clause quotes on verdict cards: taken from the writer's reply, shown only if they are really in the document.

The writer is asked to end its card with lines like `QUOTE arbitration: <clause copied word for word>`.
Those lines are always removed from the card. Each quote is then checked once against the *full* document
text (not just the excerpt the writer saw), after normalising both the same way, so differences in
whitespace, Markdown from parsed PDFs, curly quotes or dashes, and letter case don't cause false misses.
A quote that fails is dropped; the card is sent without it.
"""

from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple

MIN_CHARS = 25
MIN_WORDS = 4
MAX_SHOWN_CHARS = 400

_QUOTE_LINE = re.compile(r"^\s*(?:[-*•]\s*)?QUOTE\b(.*)$")
_KEYED = re.compile(r"^\s+([A-Za-z_ ]+?)\s*:\s*(.+)$")
_TYPOGRAPHY = str.maketrans({
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‘": "'", "’": "'", "‚": "'",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
})
# Markdown that parsed documents carry and quoted prose doesn't: emphasis, code, headings, quotes, tables.
_MARKUP = re.compile(r"[*_`#>|]")
_EDGES = "\"' .…"


class Quote(NamedTuple):
    flag: str
    text: str


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_TYPOGRAPHY)
    text = _MARKUP.sub("", text)
    return " ".join(text.split()).casefold()


def split_card(card: str, flags: set[str]) -> tuple[str, list[Quote]]:
    """Remove every QUOTE line from the writer's card and return the quotes for known flags."""
    kept: list[str] = []
    found: list[Quote] = []
    for line in card.splitlines():
        m = _QUOTE_LINE.match(line)
        if not m:
            kept.append(line)
            continue
        keyed = _KEYED.match(m.group(1))
        if keyed:
            flag = keyed.group(1).strip().lower().replace(" ", "_")
            if flag in flags:
                found.append(Quote(flag, keyed.group(2).strip()))
    return "\n".join(kept).strip(), found


def verify(quotes: list[Quote], document: str) -> list[Quote]:
    """The quotes that appear in the document (after normalising both); each is checked once."""
    doc = normalise(document)
    passed = []
    for q in quotes:
        needle = normalise(q.text).strip(_EDGES)
        if len(needle) >= MIN_CHARS and len(needle.split()) >= MIN_WORDS and needle in doc:
            passed.append(q)
    return passed


def shown(text: str) -> str:
    """A quote as displayed: trimmed to a readable length."""
    text = " ".join(text.split()).strip(_EDGES)
    return text if len(text) <= MAX_SHOWN_CHARS else text[: MAX_SHOWN_CHARS - 1].rstrip() + "…"

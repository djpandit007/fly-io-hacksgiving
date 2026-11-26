"""Decide whether a quote really appears in a stored terms document.

Quotes shown to members must be verbatim. Both sides are normalised first so that case, line breaks,
markdown decoration and typographic quotes or dashes do not count as differences.
"""

from __future__ import annotations

import re

MIN_CHARS = 25
MIN_WORDS = 4

_PUNCTUATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
    }
)
_MARKDOWN = re.compile(r"[*_`#>|]")
_WHITESPACE = re.compile(r"\s+")
_EDGE_CHARS = "\"' .…"


def normalise(text: str) -> str:
    text = text.translate(_PUNCTUATION)
    text = _MARKDOWN.sub("", text).lower()
    return _WHITESPACE.sub(" ", text).strip(_EDGE_CHARS)


def contains(quote: str, document: str) -> bool:
    needle = normalise(quote)
    if len(needle) < MIN_CHARS or len(needle.split()) < MIN_WORDS:
        return False
    return needle in normalise(document)

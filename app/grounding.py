"""Decide whether a quote really appears in a stored terms document.

Quotes shown to members must be verbatim. Both sides are normalised first so that case, line breaks,
markdown decoration and typographic quotes or dashes do not count as differences.
"""

from __future__ import annotations

import re

from app import quotes

MIN_CHARS = 25
MIN_WORDS = 4

# quotes.normalise deletes these; here they separate table cells and headings, so they become spaces.
_SEPARATORS = re.compile(r"[#>|]")
_EDGE_CHARS = "\"' .…"


def normalise(text: str) -> str:
    return quotes.normalise(_SEPARATORS.sub(" ", text)).strip(_EDGE_CHARS)


def contains(quote: str, document: str) -> bool:
    needle = normalise(quote)
    if len(needle) < MIN_CHARS or len(needle.split()) < MIN_WORDS:
        return False
    return re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", normalise(document)) is not None

"""Clause quotes: QUOTE lines never reach the card, and only quotes really in the document pass."""

import pytest

from app.quotes import Quote, normalise, shown, split_card, verify

PDF_MARKDOWN = """# Terms of Service

| Section | Text |
|---|---|
| 14.2 | **Binding** arbitration
applies to any dispute arising out of these Terms. |

The “Services” are provided as‑is — without warranty of any kind, express or implied.
"""


def test_split_card_removes_every_quote_line_and_keeps_known_flags():
    card = ("PUSH BACK\n- **Arbitration:** you lose court access\n"
            "QUOTE arbitration: Binding arbitration applies to any dispute\n"
            "- QUOTE data sharing: We may share data with partners for advertising\n"
            "QUOTE made_up_flag: anything\nQUOTE garbage")
    text, found = split_card(card, {"arbitration", "data_sharing"})
    assert "QUOTE" not in text and text.startswith("PUSH BACK") and "lose court access" in text
    assert found == [Quote("arbitration", "Binding arbitration applies to any dispute"),
                     Quote("data_sharing", "We may share data with partners for advertising")]


@pytest.mark.parametrize("quote", [
    "Binding arbitration applies to any dispute arising out of these Terms.",  # line break + bold in the PDF
    "binding ARBITRATION applies to any dispute",                              # case
    '"Services" are provided as-is - without warranty of any kind',            # curly quotes, dashes
    "“Binding arbitration applies to any dispute arising”",                    # quoted, with curly marks
])
def test_formatting_differences_still_pass(quote):
    assert verify([Quote("arbitration", quote)], PDF_MARKDOWN)


def test_table_pipes_are_ignored():
    doc = "| You agree | to binding arbitration of all claims |"
    assert verify([Quote("arbitration", "You agree to binding arbitration of all claims")], doc)


def test_quote_beyond_the_writers_excerpt_is_checked_against_the_full_text():
    doc = "x " * 8000 + "We may sell your personal data to advertisers without notice."
    assert len(doc) > 12_000
    assert verify([Quote("data_sharing", "We may sell your personal data to advertisers")], doc)


def test_invented_quote_fails():
    assert verify([Quote("arbitration", "All disputes go to binding arbitration in Delaware")], PDF_MARKDOWN) == []


def test_trivially_short_quote_fails():
    assert verify([Quote("arbitration", "the Terms")], PDF_MARKDOWN) == []
    assert verify([Quote("arbitration", "arbitration applies to")], PDF_MARKDOWN) == []


def test_each_quote_is_judged_on_its_own():
    real = Quote("arbitration", "Binding arbitration applies to any dispute")
    fake = Quote("data_sharing", "We sell your data to anyone who asks nicely")
    assert verify([real, fake], PDF_MARKDOWN) == [real]


def test_normalise_and_shown():
    assert normalise("  **Hello**\n\tWorld — “x” ") == 'hello world - "x"'
    assert shown("a " * 300).endswith("…") and len(shown("a " * 300)) <= 400

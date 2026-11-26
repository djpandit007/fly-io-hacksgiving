from app.grounding import contains

DOC = "Fine Print may change the price of a Paid Plan at any time, with notice."


def test_exact_quote_found():
    assert contains("change the price of a Paid Plan at any time", DOC)


def test_ignores_case_line_breaks_and_markdown():
    document = "We may **change**\nthe price of a | Paid Plan |"
    assert contains("we may change the price of a paid plan", document)


def test_ignores_curly_quotes_and_dashes():
    document = "You grant us a licence – worldwide — to use “your content” and the user’s data."
    quote = 'a licence - worldwide - to use "your content" and the user\'s data'
    assert contains(quote, document)
    assert contains("a licence – worldwide — to use “your content”", quote + " and more")


def test_invented_quote_rejected():
    assert not contains("we will never raise the price of any plan", DOC)


def test_too_short_rejected():
    assert "Paid Plan" in DOC
    assert not contains("at any time", DOC)  # 3 words
    assert not contains("price of a Paid Plan", "the price of a Paid Plan x")  # 5 words but 20 chars


def test_empty_inputs():
    assert not contains("", "x")
    assert not contains("a b c d e f g h", "")

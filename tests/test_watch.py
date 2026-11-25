from app.watch import diff, html_to_markdown, normalise, opt_out_days, redline, sections

OLD_HTML = """
<html><body><nav>Home | Login</nav><main>
<h1>Terms of Service</h1><p>Last updated: January 3, 2026</p>
<h2>1. Accounts</h2><p>You must be 13 or older to use the service.</p>
<h2>2. Payments</h2><p>Fees are billed monthly in advance.</p>
<h2>3. Disputes</h2><p>Disputes are resolved in the courts of California.</p>
</main><footer>(c) Acme</footer></body></html>
"""

NEW_HTML = """
<html><body><main>
<h1>Terms of Service</h1><p>Last updated: November 20, 2026</p>
<h2>1. Accounts</h2><p>You must be 13 or older to use the service.</p>
<h2>2. Payments</h2><p>Fees are billed monthly in advance. Your plan renews automatically unless cancelled.</p>
<h2>3. Arbitration</h2><p>Disputes are resolved by binding individual arbitration. You may opt out within 30 days of accepting these terms.</p>
<h2>4. Disputes</h2><p>Disputes are resolved in the courts of California.</p>
</main></body></html>
"""


def text(html: str) -> str:
    return normalise(html_to_markdown(html))


def test_markdown_drops_chrome_and_keeps_headings():
    md = html_to_markdown(OLD_HTML)
    assert "Login" not in md and "(c) Acme" not in md
    assert "## 2. Payments" in md


def test_normalise_drops_last_updated_line():
    assert "Last updated" not in text(OLD_HTML)


def test_identical_text_has_no_changes():
    assert diff(text(OLD_HTML), text(OLD_HTML)) == []


def test_only_date_changed_is_not_a_change():
    bumped = OLD_HTML.replace("January 3, 2026", "March 9, 2026")
    assert diff(text(OLD_HTML), text(bumped)) == []


def test_detects_added_and_modified_sections_and_survives_renumbering():
    changes = {(c.heading, c.kind) for c in diff(text(OLD_HTML), text(NEW_HTML))}
    assert ("2. Payments", "modified") in changes
    assert ("3. Arbitration", "added") in changes
    # "3. Disputes" became "4. Disputes" with the same body: not a change.
    assert not any("Disputes" in h for h, _ in changes)


def test_removed_section():
    new = OLD_HTML.replace("<h2>2. Payments</h2><p>Fees are billed monthly in advance.</p>", "")
    assert [(c.heading, c.kind) for c in diff(text(OLD_HTML), text(new))] == [("2. Payments", "removed")]


def test_typo_fix_is_ignored():
    fixed = OLD_HTML.replace("billed monthly", "billed  monthly").replace("in advance.", "in advance")
    assert diff(text(OLD_HTML), text(fixed)) == []


def test_sections_preamble_without_heading():
    secs = sections("intro line\n## A\nbody")
    assert secs[0].heading == "" and secs[0].body == "intro line"
    assert secs[1].heading == "A"


def test_redline_marks_insertions():
    out = redline("Fees are billed monthly.", "Fees are billed monthly. Plans renew automatically.")
    assert "{+" in out and "renew automatically." in out


def test_opt_out_days():
    assert opt_out_days("You may opt out within 30 days of accepting.") == 30
    assert opt_out_days("send notice within thirty (30) days") == 30
    assert opt_out_days("no window here") is None


def test_markdown_canonical_matches_html_shape():
    md = """Terms
=====

[](#fees)**2\\. Payments**

Fees are billed [monthly](https://x.test) in *advance*.

1. First item
* Second item
"""
    from app.watch import markdown_to_canonical
    out = normalise(markdown_to_canonical(md))
    assert out.splitlines() == [
        "## Terms", "## 2. Payments", "Fees are billed monthly in advance.", "- First item", "- Second item",
    ]


def test_bold_led_paragraph_is_a_heading():
    html = "<main><p><strong>4. Fees.</strong> You agree to pay.</p><p><b>Arbitration</b></p><p>Binding.</p></main>"
    assert html_to_markdown(html).splitlines() == ["## 4. Fees", "You agree to pay.", "## Arbitration", "Binding."]


def test_renamed_section_with_same_body_is_not_a_change():
    old = "## Disputes\nAll disputes go to the courts of California under state law."
    new = "## Resolving disputes\nAll disputes go to the courts of California under state law."
    assert diff(old, new) == []


def test_renamed_and_edited_section_is_one_modification():
    old = "## Disputes\nAll disputes go to the courts of California under state law and nothing else applies here."
    new = "## Resolving disputes\nAll disputes go to binding arbitration in California under state law and nothing else applies here."
    [c] = diff(old, new)
    assert (c.heading, c.kind) == ("Resolving disputes", "modified")


def test_quotes_and_bullets_do_not_count_as_changes():
    assert diff('## A\n- The ( " Terms " ) apply.', "## A\nThe (“Terms”) apply.") == []


def test_repeated_headings_are_compared_by_occurrence():
    old = "## User Content\nYou may post content.\n## Licence\nx\n## User Content\nYou grant us a limited licence to your content."
    assert diff(old, old) == []
    new = old.replace("a limited licence", "an irrevocable worldwide licence")
    [c] = diff(old, new)
    assert c.kind == "modified" and "irrevocable" in c.after

"""tools/ingest/convert.py: a DOCTYPE with an internal subset must not leak into the text."""

from tools.ingest import convert

SUBSET = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<!DOCTYPE html [<!ENTITY amp "&#38;#38;"><!ENTITY lt "&#60;#60;">'
    '<!ENTITY nbsp "&#160;">]>'
)


def test_the_internal_subset_does_not_leave_stray_text_behind():
    page = SUBSET + "<div><h2>Relevance</h2><p>Check the file.</p></div>"
    markdown = convert.html_to_markdown(page)
    assert "]>" not in markdown
    assert "ENTITY" not in markdown
    assert markdown.startswith("## Relevance")
    assert "Check the file." in markdown


def test_ordinary_entities_still_decode_after_the_declaration_is_removed():
    page = SUBSET + "<div><p>a &amp; b &lt; c</p></div>"
    assert "a & b < c" in convert.html_to_markdown(page)


def test_a_plain_doctype_is_unaffected():
    page = "<!DOCTYPE html><html><body><p>Hello</p></body></html>"
    assert convert.html_to_markdown(page).strip() == "Hello"


def test_a_page_with_no_doctype_is_unaffected():
    assert convert.html_to_markdown("<p>Hello</p>").strip() == "Hello"


def test_only_a_leading_declaration_is_removed():
    page = "<p>keep</p><!DOCTYPE x [ <!ENTITY y 'z'> ]><p>this</p>"
    markdown = convert.html_to_markdown(page)
    assert "keep" in markdown
    assert "this" in markdown

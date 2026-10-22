"""tools/ingest/chunking.py: markdown + template tags -> chunks and examples."""

import pytest

from tools.ingest import chunking
from tools.ingest.chunking import FORM_FENCE_QA, FORM_FENCE_RELEVANCE, FORM_QNA


def parse(text, **kwargs):
    return chunking.parse_document(text, default_title="doc", **kwargs)


def examples(text, **kwargs):
    return [ex for chunk in parse(text, **kwargs).chunks for ex in chunk.examples]


class TestQnaBlocks:
    def test_query_answers_type_and_timing(self):
        found = examples(
            '# h\n\nProse.\n\n{% qna %}\nQ: bit 1 of bit set "10"\nA: True\nI: singular boolean\n'
            "T: 0.139 ms\n{% endqna %}\n"
        )
        assert len(found) == 1
        ex = found[0]
        assert ex.form == FORM_QNA
        assert ex.query == 'bit 1 of bit set "10"'
        assert ex.answers == ("True",)
        assert ex.result_type == "singular boolean"
        assert not ex.incomplete

    def test_timing_line_is_dropped_from_the_chunk_text(self):
        chunk = parse("# h\n\n{% qna %}\nQ: x\nA: 1\nT: 12.5 ms\n{% endqna %}\n").chunks[0]
        assert "Q: x" in chunk.text
        assert "12.5" not in chunk.text

    def test_several_answers_keep_order_and_empty_values(self):
        (ex,) = examples('# h\n\n{% qna %}\nQ: q\nA: "a" "" ""\nA:\nA: last\n{% endqna %}\n')
        assert ex.answers == ('"a" "" ""', "", "last")

    def test_errors_are_captured(self):
        (ex,) = examples(
            "# h\n\n{% qna %}\nQ: bad\nE: A singular expression is required.\n{% endqna %}\n"
        )
        assert ex.errors == ("A singular expression is required.",)
        assert ex.answers == ()

    def test_each_question_in_a_block_is_its_own_example(self):
        found = examples("# h\n\n{% qna %}\nQ: one\nA: 1\nQ: two\nA: 2\nA: 3\n{% endqna %}\n")
        assert [(e.query, e.answers) for e in found] == [("one", ("1",)), ("two", ("2", "3"))]

    def test_missing_space_after_the_colon_is_accepted_like_the_site_parser(self):
        (ex,) = examples('# h\n\n{% qna %}\nQ:pid of service "sshd"\nA: 1104\n{% endqna %}\n')
        assert ex.query == 'pid of service "sshd"'

    def test_a_windows_path_is_not_a_query(self):
        text = "# h\n\n```\nQ:\\data\\file.txt\nQ:/unix/path\n```\n"
        assert examples(text) == []

    def test_an_elision_line_marks_the_example_incomplete(self):
        (ex,) = examples("# h\n\n{% qna %}\nQ: q\nA: first\n...\n{% endqna %}\n")
        assert ex.incomplete
        assert ex.answers == ("first",)

    def test_indented_block_inside_html_is_parsed(self):
        text = (
            "# h\n\n<ol>\n<li>Step\n  {% qna %}\n  Q: (exists a)\n  A: True\n  {% endqna %}\n"
            "</li>\n</ol>\n"
        )
        (ex,) = examples(text)
        assert (ex.query, ex.answers) == ("(exists a)", ("True",))

    def test_answer_text_is_not_html_unescaped(self):
        (ex,) = examples(
            "# h\n\n{% qna %}\nQ: q\nA: logged on user of <user>: &lt;x&gt;\n{% endqna %}\n"
        )
        assert ex.answers == ("logged on user of <user>: &lt;x&gt;",)

    def test_an_answer_without_a_question_makes_no_example(self):
        assert examples("# h\n\n{% qna %}\nA: orphan\n{% endqna %}\n") == []

    def test_unclosed_block_is_kept(self):
        (ex,) = examples("# h\n\n{% qna %}\nQ: q\nA: 1\n")
        assert ex.query == "q"


class TestFences:
    def test_relevance_fence_is_a_query_without_an_answer(self):
        (ex,) = examples("# h\n\n```relevance\nnames of bes computers  \n```\n\nReturns names.\n")
        assert ex.form == FORM_FENCE_RELEVANCE
        assert ex.query == "names of bes computers"
        assert ex.answers == ()
        assert ex.after == "Returns names."

    def test_multi_line_relevance_fence_stays_one_query(self):
        (ex,) = examples("# h\n\n```relevance\n(name of it,\n id of it) of bes computers\n```\n")
        assert ex.query == "(name of it,\n id of it) of bes computers"

    def test_blank_lines_separate_the_queries_in_one_fence(self):
        text = "# h\n\n```relevance\nnumber of bes computers\n\n  \nnames of bes sites\n```\n"
        parsed = parse(text)
        found = [ex for chunk in parsed.chunks for ex in chunk.examples]
        assert [e.query for e in found] == ["number of bes computers", "names of bes sites"]
        assert all(e.form == FORM_FENCE_RELEVANCE for e in found)
        # the chunk text still shows the fence exactly as written
        assert parsed.chunks[0].text.count("```") == 2

    def test_a_relevance_fence_of_only_blank_lines_makes_no_example(self):
        assert examples("# h\n\n```relevance\n\n\n```\n") == []

    def test_plain_fence_with_q_and_a_lines_gives_cases(self):
        text = (
            "# h\n\n```\n// Only defined for Apple version strings\n"
            'Q: bug revision of version "1.2.3.4"\n'
            'E: The operator "bug revision" is not defined.\n\n'
            'Q: bug revision of version "1.2.3a4"\n'
            "A: 3\n```\n"
        )
        found = examples(text)
        assert [e.form for e in found] == [FORM_FENCE_QA, FORM_FENCE_QA]
        assert found[0].errors == ('The operator "bug revision" is not defined.',)
        assert found[1].answers == ("3",)
        assert not any(e.incomplete for e in found)

    def test_other_languages_are_text_not_examples(self):
        chunk = parse("# h\n\n```sql\nselect 1\n```\n").chunks[0]
        assert chunk.examples == []
        assert "select 1" in chunk.text

    def test_plain_fence_without_q_lines_is_not_an_example(self):
        assert examples("# h\n\n```\njust output\n```\n") == []

    def test_heading_like_lines_inside_fences_and_blocks_are_not_headings(self):
        text = "# real\n\n```\n# not a heading\n```\n\n{% qna %}\nQ: q\n# also not\n{% endqna %}\n"
        parsed = parse(text)
        assert [c.heading for c in parsed.chunks] == ["real"]

    def test_longer_fence_is_closed_only_by_an_equal_or_longer_one(self):
        text = "# h\n\n````\n```\ninner\n```\n````\n\nafter\n"
        chunk = parse(text).chunks[0]
        assert "inner" in chunk.text
        assert chunk.text.endswith("after")

    def test_tilde_fences_work(self):
        (ex,) = examples("# h\n\n~~~relevance\nnumber of bes computers\n~~~\n")
        assert ex.query == "number of bes computers"

    def test_unclosed_fence_is_kept(self):
        (ex,) = examples("# h\n\n```relevance\nnumber of bes computers\n")
        assert ex.query == "number of bes computers"


class TestContext:
    def test_before_and_after_are_the_adjacent_paragraphs(self):
        text = "# h\n\nBefore para.\n\n{% qna %}\nQ: q\nA: 1\n{% endqna %}\n\nAfter para.\n"
        (ex,) = examples(text)
        assert ex.before == "Before para."
        assert ex.after == "After para."

    def test_context_does_not_cross_a_heading(self):
        text = "# a\n\nIn a.\n\n# b\n\n```relevance\nx\n```\n"
        (ex,) = examples(text)
        assert ex.before == ""

    def test_long_context_is_clipped(self):
        text = "# h\n\n" + ("word " * 300) + "\n\n```relevance\nx\n```\n"
        (ex,) = examples(text)
        assert len(ex.before) == chunking.CONTEXT_CHARS


class TestProse:
    def test_bom_and_front_matter_title(self):
        parsed = parse("\ufeff---\ntitle: Computer Inspectors\n---\n\nBody text.\n")
        assert parsed.title == "Computer Inspectors"
        assert parsed.chunks[0].heading == "Computer Inspectors"
        assert parsed.chunks[0].text == "Body text."

    def test_a_leading_rule_is_not_front_matter(self):
        text = "---\n\nSome prose here.\n\n---\n\nmore\n"
        parsed = parse(text)
        assert parsed.title == "doc"
        assert "Some prose here." in parsed.chunks[0].text

    def test_heading_entities_are_unescaped(self):
        parsed = parse("# bit &lt;integer&gt; of &lt;bit set&gt; : boolean\n\nReturns a bit.\n")
        assert parsed.chunks[0].heading == "bit <integer> of <bit set> : boolean"

    @pytest.mark.parametrize(
        "expression",
        ["{{'file' | linkType}}", '{{ "file" | linkType }}', "{{ 'file'|linkType }}"],
    )
    def test_the_link_type_filter_renders_as_its_text(self, expression):
        parsed = parse("# h\n\nIt defines a " + expression + " object.\n")
        assert parsed.chunks[0].text == "It defines a file object."
        assert parsed.leftover_templates == 0

    def test_the_link_type_filter_works_in_headings_and_with_spaces_in_the_name(self):
        parsed = parse("# the {{'bes computer' | linkType}} type\n\nBody.\n")
        assert parsed.chunks[0].heading == "the bes computer type"
        assert parsed.leftover_templates == 0

    def test_template_syntax_inside_code_is_left_verbatim_and_not_counted(self):
        parsed = parse("# h\n\n```\n{{'file' | linkType}}\n```\n")
        assert "{{'file' | linkType}}" in parsed.chunks[0].text
        assert parsed.leftover_templates == 0

    def test_unrecognised_template_expressions_stay_visible_and_are_counted(self):
        text = "# h\n\nUses {{ other('x') }} and {{ 'y' | upper }} here.\n"
        parsed = parse(text)
        assert "{{ other('x') }}" in parsed.chunks[0].text
        assert "{{ 'y' | upper }}" in parsed.chunks[0].text  # only linkType is known
        assert parsed.leftover_templates == 2

    def test_a_clean_document_has_no_leftover_templates(self):
        assert parse("# h\n\n{% note %}\nPlain.\n{% endnote %}\n").leftover_templates == 0

    def test_html_tags_are_removed_but_placeholders_survive(self):
        text = "# h\n\n<strong>Example</strong> of <user> and <bit set> and <integer> values.\n"
        assert parse(text).chunks[0].text == "Example of <user> and <bit set> and <integer> values."

    def test_html_comments_are_removed_even_across_lines(self):
        text = (
            "# h\n\nKeep <!-- inline --> this.\n\n"
            "<!--\n  * stale bullet\n  * another\n-->\n\nAnd this.\n"
        )
        body = parse(text).chunks[0].text
        assert "stale" not in body
        assert "Keep  this." in body
        assert "And this." in body

    def test_comment_markers_inside_a_fence_are_kept(self):
        chunk = parse("# h\n\n```xml\n<!-- keep -->\n<a/>\n```\n").chunks[0]
        assert "<!-- keep -->" in chunk.text

    def test_template_tag_lines_are_dropped_and_their_content_kept(self):
        text = (
            "# h\n\n{% section %}\nIntro.\n\n{% note %}\nCareful.\n{% endnote %}\n\n"
            '{% collapse "Show Answers" %}\nversions of packages "x" of rpm\n{% endcollapse %}\n'
        )
        body = parse(text).chunks[0].text
        assert "{%" not in body
        assert "Intro." in body
        assert "Careful." in body
        assert 'versions of packages "x" of rpm' in body

    def test_links_and_images_become_their_text(self):
        text = "# h\n\nSee [the guide](./guide.html) and ![Dialog](/static/a.png).\n"
        assert parse(text).chunks[0].text == "See the guide and Dialog."

    def test_crlf_input(self):
        parsed = parse("# h\r\n\r\n{% qna %}\r\nQ: q\r\nA: 1\r\n{% endqna %}\r\n")
        assert parsed.chunks[0].examples[0].answers == ("1",)


class TestSections:
    def test_section_path_follows_the_heading_nesting(self):
        text = "# Guide\n\nIntro.\n\n## A\n\nText a.\n\n### A1\n\nText a1.\n\n## B\n\nText b.\n"
        paths = {c.heading: c.section_path for c in parse(text).chunks}
        assert paths["A1"] == ("doc", "Guide", "A", "A1")
        assert paths["B"] == ("doc", "Guide", "B")

    def test_a_first_heading_equal_to_the_title_is_not_repeated(self):
        parsed = parse("---\ntitle: Guide\n---\n\n# Guide\n\n## A\n\nText.\n")
        assert parsed.chunks[-1].section_path == ("Guide", "A")

    def test_sibling_top_level_headings_each_get_a_chunk(self):
        text = "# one : integer\n\nFirst.\n\n# two : integer\n\nSecond.\n"
        assert [c.heading for c in parse(text).chunks] == ["one : integer", "two : integer"]

    def test_text_before_the_first_heading_gets_the_title(self):
        chunk = parse("Preface text.\n\n# H\n\nBody.\n").chunks[0]
        assert chunk.heading == "doc"
        assert chunk.text == "Preface text."

    def test_stub_sections_are_skipped_and_counted(self):
        text = "# a : integer\n\nNo documentation exists.\n\n# b : integer\n\nReal text.\n"
        parsed = parse(text)
        assert parsed.stub_skipped == 1
        assert [c.heading for c in parsed.chunks] == ["b : integer"]

    def test_a_stub_with_an_example_is_kept(self):
        text = "# a : integer\n\nNo documentation exists.\n\n```\nQ: 1 + 1\nA: 2\n```\n"
        parsed = parse(text)
        assert parsed.stub_skipped == 0
        assert len(parsed.chunks[0].examples) == 1

    def test_a_heading_with_no_body_is_counted_but_an_empty_preamble_is_not(self):
        parsed = parse("# parent\n\n## child\n\nText.\n")
        assert parsed.empty_skipped == 1
        assert [c.heading for c in parsed.chunks] == ["child"]

    def test_an_empty_document_has_no_chunks(self):
        parsed = parse("")
        assert parsed.chunks == []
        assert parsed.empty_skipped == 0


class TestSplitting:
    def test_a_long_section_is_split_at_block_boundaries(self):
        paragraphs = [f"Paragraph {n} " + "x" * 80 for n in range(30)]
        parsed = parse("# h\n\n" + "\n\n".join(paragraphs) + "\n", max_chars=500)
        assert len(parsed.chunks) > 1
        assert all(len(c.text) <= 500 for c in parsed.chunks)
        assert all(c.heading == "h" for c in parsed.chunks)
        rejoined = "\n\n".join(c.text for c in parsed.chunks)
        assert all(p in rejoined for p in paragraphs)

    def test_a_code_block_is_never_split_and_keeps_its_example(self):
        code = "\n".join(f"line {n} " + "y" * 60 for n in range(20))
        text = f"# h\n\nIntro.\n\n```relevance\n{code}\n```\n\nOutro.\n"
        parsed = parse(text, max_chars=400)
        holders = [c for c in parsed.chunks if c.examples]
        assert len(holders) == 1
        assert code in holders[0].text
        assert holders[0].examples[0].query == code

    def test_an_over_long_paragraph_is_cut_at_lines(self):
        body = "\n".join("z" * 90 for _ in range(30))
        parsed = parse(f"# h\n\n{body}\n", max_chars=500)
        assert len(parsed.chunks) > 1
        assert all(len(c.text) <= 500 for c in parsed.chunks)
        assert "".join(c.text.replace("\n", "") for c in parsed.chunks) == body.replace("\n", "")

    def test_a_single_huge_line_is_hard_cut(self):
        parsed = parse("# h\n\n" + "q" * 1200 + "\n", max_chars=500)
        assert [len(c.text) for c in parsed.chunks] == [500, 500, 200]


@pytest.mark.parametrize(
    ("line", "matches"),
    [
        ("Q: x", True),
        ("Q:", True),
        ("Q:x", True),
        ("Q:\\x", False),
        ("Q:/x", False),
        ("Z: x", False),
    ],
)
def test_field_line_grammar(line, matches):
    assert bool(chunking._FIELD_RE.match(line)) is matches

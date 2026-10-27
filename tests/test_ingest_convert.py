"""tools/ingest/convert.py: HTML and PDF to markdown."""

import sys
import types

import pytest

from tests.ingest_pdf import make_pdf
from tools.ingest import convert


def md(html):
    return convert.html_to_markdown(html)


class TestHtml:
    def test_headings_paragraphs_and_lists(self):
        out = md(
            "<html><body><h1>Title</h1><p>First para.</p><h2>Sub</h2>"
            "<ul><li>one</li><li>two</li></ul><p>Last.</p></body></html>"
        )
        assert out == "# Title\n\nFirst para.\n\n## Sub\n\n- one\n- two\n\nLast.\n"

    def test_pre_becomes_a_fenced_block_and_keeps_its_layout(self):
        out = md(
            "<body><p>Run:</p><pre><code>names of bes computers\n  whose x</code></pre></body>"
        )
        assert "```\nnames of bes computers\n  whose x\n```" in out
        assert "`names" not in out

    def test_inline_code_gets_backticks(self):
        assert "use `it` here" in md("<body><p>use <code>it</code> here</p></body>")

    def test_navigation_scripts_and_styles_are_dropped(self):
        out = md(
            "<body><nav>Home Guides</nav><script>var x = 1;</script><style>p{}</style>"
            "<header>Banner</header><p>Real text.</p><footer>Copyright</footer></body>"
        )
        assert out == "Real text.\n"

    def test_only_main_is_kept_when_present(self):
        out = md(
            "<body><div>Side menu</div><main><h1>Doc</h1><p>Body.</p></main><div>Ad</div></body>"
        )
        assert out == "# Doc\n\nBody.\n"

    def test_the_title_becomes_a_heading_only_when_there_is_no_h1(self):
        assert md("<head><title>Page Name</title></head><body><p>Text.</p></body>").startswith(
            "# Page Name"
        )
        assert md("<head><title>Page Name</title></head><body><h1>Real</h1></body>") == "# Real\n"

    def test_entities_are_decoded(self):
        assert md("<body><p>a &lt; b &amp; c</p></body>") == "a < b & c\n"

    def test_table_cells_are_separated(self):
        out = md("<body><table><tr><td>name</td><td>value</td></tr></table></body>")
        assert "name" in out and "value" in out
        assert "namevalue" not in out

    def test_malformed_html_does_not_raise(self):
        assert isinstance(md("<p>unclosed <b>bold <div>mess</i></p>"), str)

    def test_empty_input_gives_empty_output(self):
        assert md("") == ""


class TestCleanPdfPages:
    def test_typographic_quotes_become_straight_quotes(self):
        (page,) = convert.clean_pdf_pages(
            ["Pathname of regapp \u201cbde.exe\u201d and let\u2019s \u2018go\u2019"]
        )
        assert page == "Pathname of regapp \"bde.exe\" and let's 'go'"

    def test_control_characters_and_nbsp_are_cleaned(self):
        (page,) = convert.clean_pdf_pages(['\x84\x84  exists folder\u00a0"fonts"'])
        assert page == '  exists folder "fonts"'.strip()

    def test_running_footer_and_page_numbers_are_removed_content_kept(self):
        pages = [f"Content of page {n}\n\u00a92005 by BigFix, Inc.\n{n}" for n in range(1, 7)]
        cleaned = convert.clean_pdf_pages(pages)
        assert cleaned == [f"Content of page {n}" for n in range(1, 7)]

    def test_only_this_pages_own_number_is_removed(self):
        pages = [f"first line\n{n if n != 3 else 7}\nlast line" for n in range(1, 7)]
        cleaned = convert.clean_pdf_pages(pages)
        assert "7" in cleaned[2].split("\n")  # page 3 shows a different number: content
        assert "1" not in cleaned[0].split("\n")

    def test_a_line_repeated_on_half_the_pages_is_content(self):
        pages = [("Description\n" if n % 2 else "") + f"row {n}" for n in range(1, 11)]
        cleaned = convert.clean_pdf_pages(pages)
        assert sum("Description" in page for page in cleaned) == 5

    def test_the_threshold_is_ninety_percent_of_populated_pages(self):
        nine_of_ten = [("Footer\n" if n < 9 else "") + f"row {n}" for n in range(10)]
        assert not any("Footer" in page for page in convert.clean_pdf_pages(nine_of_ten))
        eight_of_ten = [("Footer\n" if n < 8 else "") + f"row {n}" for n in range(10)]
        assert sum("Footer" in page for page in convert.clean_pdf_pages(eight_of_ten)) == 8

    def test_blank_pages_do_not_count_toward_the_share(self):
        # 5 populated pages share a footer; 5 blank ones must not dilute that below 90%
        pages = [f"row {n}\nFooter" if n < 5 else "" for n in range(10)]
        cleaned = convert.clean_pdf_pages(pages)
        assert all("Footer" not in page for page in cleaned)
        assert cleaned[5:] == [""] * 5

    def test_too_few_pages_means_no_header_detection(self):
        pages = [f"row {n}\nFooter" for n in range(4)]
        assert all("Footer" in page for page in convert.clean_pdf_pages(pages))

    def test_a_page_that_was_only_boilerplate_ends_up_empty(self):
        pages = [f"row {n}\nFooter" for n in range(5)] + ["Footer"]
        cleaned = convert.clean_pdf_pages(pages)
        assert cleaned[-1] == ""


class TestPdf:
    def test_cleaning_is_applied_to_the_extracted_pages(self, monkeypatch, tmp_path):
        texts = ["Hello \u201cthere\u201d\nFooter\n1", "", "Next\nFooter\n3"] + [
            f"Page {n}\nFooter\n{n}" for n in range(4, 8)
        ]

        class Page:
            def __init__(self, text) -> None:
                self._text = text

            def extract_text(self):
                return self._text

        class Reader:
            def __init__(self, path) -> None:
                self.pages = [Page(t) for t in texts]

        monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=Reader))
        out = convert.pdf_to_markdown(tmp_path / "x.pdf")
        assert out.startswith('## Page 1\n\nHello "there"\n\n## Page 3\n\nNext\n\n## Page 4')
        assert "Footer" not in out
        assert "## Page 2" not in out

    def test_missing_pypdf_raises_the_dedicated_error(self, monkeypatch, tmp_path):
        monkeypatch.setitem(sys.modules, "pypdf", None)  # makes `import pypdf` raise ImportError
        with pytest.raises(convert.ConverterUnavailableError, match="pypdf"):
            convert.pdf_to_markdown(tmp_path / "x.pdf")

    def test_pages_become_sections_and_empty_pages_are_skipped(self, monkeypatch, tmp_path):
        class Page:
            def __init__(self, text) -> None:
                self._text = text

            def extract_text(self):
                return self._text

        class Reader:
            def __init__(self, path) -> None:
                self.pages = [Page("first page"), Page("   "), Page(None), Page("fourth page")]

        monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=Reader))
        out = convert.pdf_to_markdown(tmp_path / "x.pdf")
        assert out == "## Page 1\n\nfirst page\n\n## Page 4\n\nfourth page\n"

    def test_real_pypdf_extracts_text_page_by_page(self, tmp_path):
        pytest.importorskip("pypdf")
        path = tmp_path / "real.pdf"
        path.write_bytes(make_pdf(["Hello relevance", "", "Third (page)"]))
        out = convert.pdf_to_markdown(path)
        assert out == "## Page 1\n\nHello relevance\n\n## Page 3\n\nThird (page)\n"

    def test_a_pdf_with_no_text_gives_empty_output(self, monkeypatch, tmp_path):
        class Reader:
            def __init__(self, path) -> None:
                self.pages = []

        monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=Reader))
        assert convert.pdf_to_markdown(tmp_path / "x.pdf") == ""

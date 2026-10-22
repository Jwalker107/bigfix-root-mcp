"""Convert fetched HTML and PDF sources to markdown for chunking.

HTML uses only the standard library. If the page has <main> or <article>, only
that is kept; otherwise the body, minus navigation, scripts and similar
furniture. The output is plain structural markdown: headings, paragraphs, list
items, and fenced <pre> blocks.

PDF needs `pypdf`, a dev-only dependency of this project (the server never
imports it). When it is not installed, conversion raises
ConverterUnavailableError and the build skips that source with a clear message
instead of failing. Extracted PDF text has no headings, so each page becomes a
`## Page N` section. PDF text is also normalised: typographic quotes become
straight quotes (relevance only accepts those), glyph-mapping control characters
are removed, and running headers, footers and page numbers are dropped.

Fetched content is untrusted data. Nothing here evaluates or follows it.
"""

import re
from collections import Counter
from collections.abc import Sequence
from html.parser import HTMLParser
from pathlib import Path


class ConverterUnavailableError(RuntimeError):
    """A converter's optional dependency is not installed."""


_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "template"}
)
_VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "wbr", "area", "base", "col"})
_BLOCK_TAGS = frozenset(
    {"p", "div", "section", "article", "main", "ul", "ol", "table", "tr", "dl", "dt", "dd",
     "blockquote", "figure", "figcaption", "details", "summary"}
)  # fmt: skip
_HEADINGS = {f"h{n}": n for n in range(1, 7)}


class _Converter(HTMLParser):
    def __init__(self, *, scoped: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.has_h1 = False
        self._scoped = scoped  # only emit inside <main>/<article>
        self._scope_depth = 0
        self._skip_depth = 0
        self._pre_depth = 0
        self._in_title = False
        self._heading: int | None = None
        self._open_tags: list[str] = []

    # emission is allowed outside any skipped element, and (when scoped) inside the scope
    def _emitting(self) -> bool:
        return self._skip_depth == 0 and (not self._scoped or self._scope_depth > 0)

    def _break(self, blank: bool = True) -> None:
        """End the current line (or paragraph, if blank), adding only the missing newlines."""
        if not (self._emitting() and self.parts):
            return
        tail = "".join(self.parts[-3:])
        present = len(tail) - len(tail.rstrip("\n"))
        missing = (2 if blank else 1) - present
        if missing > 0:
            self.parts.append("\n" * missing)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if tag in ("main", "article") and self._scoped:
            self._scope_depth += 1
        if not self._emitting():
            return
        if tag in _HEADINGS:
            self._break()
            self._heading = _HEADINGS[tag]
            self.parts.append("#" * self._heading + " ")
            if self._heading == 1:
                self.has_h1 = True
        elif tag == "pre":
            self._break()
            self._pre_depth += 1
            self.parts.append("```\n")
        elif tag == "li":
            self._break(blank=False)
            self.parts.append("- ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag == "code" and not self._pre_depth:
            self.parts.append("`")
        elif tag in _BLOCK_TAGS:
            self._break()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS and self._skip_depth:
                self._skip_depth -= 1
            return
        was_emitting = self._emitting()
        if tag in ("main", "article") and self._scoped and self._scope_depth:
            self._scope_depth -= 1
        if not was_emitting:
            return
        if tag in _HEADINGS:
            self._heading = None
            self._break()
        elif tag == "pre" and self._pre_depth:
            self._pre_depth -= 1
            self.parts.append("\n```")
            self._break()
        elif tag == "code" and not self._pre_depth:
            self.parts.append("`")
        elif tag in _BLOCK_TAGS or tag == "li":
            self._break(blank=tag != "li")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if not self._emitting():
            return
        if self._pre_depth:
            self.parts.append(data)
        else:
            collapsed = re.sub(r"\s+", " ", data)
            if collapsed.strip() or (self.parts and not self.parts[-1].endswith(("\n", " "))):
                self.parts.append(collapsed)


def html_to_markdown(source: str) -> str:
    """Convert an HTML document to markdown. Never raises on malformed HTML."""
    scoped = bool(re.search(r"<(?:main|article)\b", source, re.IGNORECASE))
    converter = _Converter(scoped=scoped)
    converter.feed(source)
    converter.close()
    text = "".join(converter.parts)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    title = " ".join(converter.title.split())
    if title and not converter.has_h1:
        text = f"# {title}\n\n{text}".strip()
    return text + "\n" if text else ""


# Word-processed PDFs carry typographic punctuation. Relevance only accepts
# straight quotes, so an example copied out of such a PDF is wrong until they are
# normalised. NBSP becomes a plain space.
_TYPOGRAPHY = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00a0": " ",
    }
)
# C1 control characters are glyph-mapping artifacts: the 2005 guide's Wingdings
# bullets arrive as U+0084 in front of each example line.
_C1_CONTROLS = re.compile("[\u0080-\u009f]")

# A line printed on at least this share of the pages is a running header or
# footer. Deliberately high: repeated table headings and platform lists are real
# content and sit well below it (the highest in the 239-page core guide is 49%).
HEADER_FOOTER_SHARE = 0.9
# with fewer pages than this "most of them" means nothing
MIN_PAGES_FOR_HEADER_DETECTION = 5


def clean_pdf_pages(pages: Sequence[str]) -> list[str]:
    """Normalise extracted PDF page text and drop running headers, footers and page numbers.

    Only text that is not content is removed: lines repeated on at least
    HEADER_FOOTER_SHARE of the non-empty pages, and a line that is just this
    page's number. Everything else is kept as extracted.
    """
    normalised = [_C1_CONTROLS.sub("", page.translate(_TYPOGRAPHY)) for page in pages]
    populated = [page for page in normalised if page.strip()]
    boilerplate: set[str] = set()
    if len(populated) >= MIN_PAGES_FOR_HEADER_DETECTION:
        seen_on = Counter(
            line for page in populated for line in {ln.strip() for ln in page.split("\n")} if line
        )
        boilerplate = {
            line for line, count in seen_on.items() if count >= HEADER_FOOTER_SHARE * len(populated)
        }
    cleaned = []
    for number, page in enumerate(normalised, start=1):
        kept = [
            line
            for line in page.split("\n")
            if line.strip() not in boilerplate and line.strip() != str(number)
        ]
        cleaned.append(re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip())
    return cleaned


def pdf_to_markdown(path: Path) -> str:
    """Extract a PDF's text as markdown, one `## Page N` section per non-empty page.

    The text is normalised first (see clean_pdf_pages).
    """
    try:
        import pypdf  # noqa: PLC0415 - optional dependency, imported only when needed
    except ImportError as err:
        raise ConverterUnavailableError(
            "pypdf is not installed; install it (it is in the dev dependency group)"
        ) from err
    reader = pypdf.PdfReader(str(path))
    texts = clean_pdf_pages([page.extract_text() or "" for page in reader.pages])
    sections = [f"## Page {n}\n\n{text}" for n, text in enumerate(texts, start=1) if text]
    return "\n\n".join(sections) + ("\n" if sections else "")

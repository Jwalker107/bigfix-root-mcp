"""Parse relevance documentation markdown into searchable chunks and examples.

The input is the markdown used by developer.bigfix.com: ordinary markdown plus
a few template tags and some inline HTML. This module turns one document into

- chunks: one per heading section (long sections are split at block
  boundaries), with the section path, so a hit can say where it came from; and
- examples: the runnable or illustrative relevance found in the document, each
  with the prose next to it.

Examples come from three forms:

  qna              {% qna %} ... {% endqna %} blocks. Lines are `Q:` query,
                   `A:` expected answer (repeated for several rows), `E:`
                   expected error, `I:` result type and `T:` evaluation time.
                   The site's own parser keeps only the last `Q:` of a block;
                   here every `Q:` starts its own case. `T:` is dropped.
  fence-qa         a fenced code block whose lines use the same Q:/A:/E: form.
  fence-relevance  a fenced block tagged `relevance`: a query with no answer.

Example text is never HTML-unescaped or reformatted: `<user>` in an answer is
literal. Prose is cleaned (template tags, known HTML tags, entities, markdown
links) because it is for reading and search, not for evaluation.

Pure functions only: no I/O, so everything here is unit-testable.
"""

import html
import re
import textwrap
from dataclasses import dataclass, field

MAX_CHUNK_CHARS = 3000
CONTEXT_CHARS = 400

FORM_QNA = "qna"
FORM_FENCE_QA = "fence-qa"
FORM_FENCE_RELEVANCE = "fence-relevance"

_QNA_OPEN_RE = re.compile(r"^\s*\{%-?\s*qna\s*-?%\}\s*$")
_QNA_CLOSE_RE = re.compile(r"^\s*\{%-?\s*endqna\s*-?%\}\s*$")
_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
# `Q: text`, or a bare `Q:`. A drive letter such as `Q:\data` must not match.
_FIELD_RE = re.compile(r"^([QAIET]):(?:$| (.*)$|([^\s\\/].*)$)")
_STUB_RE = re.compile(r"^no documentation exists\.?$", re.IGNORECASE)

_TEMPLATE_RE = re.compile(r"\{%.*?%\}")
# `{{ 'file' | linkType }}` renders as a link whose text is the quoted string
# (site/build/lib/renderer.js), so the text is what a reader sees. It is the only
# `{{ }}` form in the pinned docs; any other is left visible and counted, never
# guessed at.
_LINK_TYPE_RE = re.compile(r"\{\{\s*(['\"])(.+?)\1\s*\|\s*linkType\s*\}\}")
# template syntax still present in prose after cleaning
_LEFTOVER_TEMPLATE_RE = re.compile(r"\{\{|\{%")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BLOCK_TAGS = "p|div|br|li|ol|ul|tr|table|dl|dt|dd|h[1-6]|hr|blockquote|pre|section"
_CELL_TAGS = "td|th"
_INLINE_TAGS = "span|em|strong|b|i|a|code|u|sup|sub|small|font|center|thead|tbody|img"
# Only these element names are treated as HTML. A placeholder such as `<user>` or
# `<bit set>` is not in the list and survives.
_HTML_TAG_RE = re.compile(
    rf"</?(?:(?P<block>{_BLOCK_TAGS})|(?P<cell>{_CELL_TAGS})|(?P<inline>{_INLINE_TAGS}))"
    r"(?=[\s/>])[^>]*>",
    re.IGNORECASE,
)
_FRONT_MATTER_LINE_RE = re.compile(r"^(?:[A-Za-z_][\w-]*:(?:\s.*)?|\s+.*|-\s.*|#.*|)$")
_TITLE_RE = re.compile(r"^title:\s*(.*?)\s*$", re.MULTILINE)


@dataclass
class Example:
    """One example. `before`/`after` are the prose immediately around it."""

    query: str
    answers: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    result_type: str = ""
    # a line in the block could not be read as Q/A/E/I/T (e.g. a `...` elision),
    # so the recorded answers may be partial and must not be scored exactly
    incomplete: bool = False
    form: str = FORM_QNA
    before: str = ""
    after: str = ""


@dataclass
class Chunk:
    heading: str
    section_path: tuple[str, ...]
    text: str
    examples: list[Example] = field(default_factory=list)


@dataclass
class ParsedDocument:
    title: str
    chunks: list[Chunk]
    # sections skipped because the body was only "No documentation exists."
    stub_skipped: int = 0
    # sections skipped because they had no body (a heading directly above another)
    empty_skipped: int = 0
    # `{{` or `{%` still in the headings and prose that were indexed (code and
    # examples are verbatim and not counted). Non-zero means a template construct
    # the cleaner does not know, and it will show up in search results.
    leftover_templates: int = 0


@dataclass
class _Block:
    kind: str  # "para" or "code"
    text: str
    examples: list[Example] = field(default_factory=list)


@dataclass
class _Section:
    heading: str
    section_path: tuple[str, ...]
    blocks: list[_Block] = field(default_factory=list)


@dataclass
class _Case:
    query: str
    answers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    result_type: str = ""
    incomplete: bool = False


def _tag_replacement(match: re.Match) -> str:
    if match.group("block"):
        return "\n"
    if match.group("cell"):
        return " "
    return ""


def plain_text(text: str) -> str:
    """Turn a prose fragment into plain text for reading and search."""
    text = _LINK_TYPE_RE.sub(r"\2", text)
    text = _TEMPLATE_RE.sub("", text)
    text = _HTML_TAG_RE.sub(_tag_replacement, text)
    text = _IMAGE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = html.unescape(text)
    lines = [line.rstrip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _clip(text: str, *, tail: bool) -> str:
    flat = " ".join(text.split())
    if len(flat) <= CONTEXT_CHARS:
        return flat
    return flat[-CONTEXT_CHARS:] if tail else flat[:CONTEXT_CHARS]


def _strip_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """Remove HTML comments from one line, tracking a comment that spans lines."""
    out: list[str] = []
    pos = 0
    while pos < len(line):
        if in_comment:
            end = line.find("-->", pos)
            if end < 0:
                return "".join(out), True
            pos = end + 3
            in_comment = False
        else:
            start = line.find("<!--", pos)
            if start < 0:
                out.append(line[pos:])
                break
            out.append(line[pos:start])
            pos = start + 4
            in_comment = True
    return "".join(out), in_comment


def split_front_matter(text: str) -> tuple[str, str]:
    """Return (title, body). The title is "" when there is no usable front matter.

    A leading `---` only counts as front matter when everything up to the closing
    `---` looks like YAML, so a page that opens with a horizontal rule is safe.
    """
    text = text.lstrip("\ufeff")
    if not text.startswith("---\n"):
        return "", text
    lines = text.split("\n")
    for end in range(1, len(lines)):
        if lines[end].rstrip() == "---":
            block = lines[1:end]
            if not all(_FRONT_MATTER_LINE_RE.match(line) for line in block):
                return "", text
            match = _TITLE_RE.search("\n".join(block))
            title = match.group(1).strip("\"'") if match else ""
            return title, "\n".join(lines[end + 1 :])
    return "", text


def parse_cases(lines: list[str]) -> list[Example]:
    """Read Q/A/E/I/T lines into one Example per `Q:` line (form is set by the caller)."""
    cases: list[_Case] = []
    current: _Case | None = None
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("//"):
            continue
        match = _FIELD_RE.match(stripped)
        if not match:
            if current is not None:
                current.incomplete = True
            continue
        tag = match.group(1)
        value = (match.group(2) or match.group(3) or "").strip()
        if tag == "Q":
            current = _Case(query=value)
            cases.append(current)
        elif current is None:
            continue  # an answer with no question: nothing to attach it to
        elif tag == "A":
            current.answers.append(value)
        elif tag == "E":
            current.errors.append(value)
        elif tag == "I":
            current.result_type = value
        # tag == "T" is an evaluation time: noise, deliberately dropped
    return [
        Example(
            query=case.query,
            answers=tuple(case.answers),
            errors=tuple(case.errors),
            result_type=case.result_type,
            incomplete=case.incomplete,
        )
        for case in cases
        if case.query
    ]


def _has_field_lines(lines: list[str], tag: str) -> bool:
    return any((m := _FIELD_RE.match(line.strip())) and m.group(1) == tag for line in lines)


def _render_qna(lines: list[str]) -> str:
    kept = []
    for line in lines:
        stripped = line.strip()
        match = _FIELD_RE.match(stripped)
        if stripped and not (match and match.group(1) == "T"):
            kept.append(stripped)
    return "```\n" + "\n".join(kept) + "\n```"


def _fence_block(lang: str, lines: list[str]) -> _Block:
    code = textwrap.dedent("\n".join(lines)).strip("\n")
    rendered = f"```{lang}\n{code}\n```"
    examples: list[Example] = []
    if _has_field_lines(lines, "Q"):
        if lang in ("", "relevance", "qna"):
            examples = parse_cases(lines)
            for example in examples:
                example.form = FORM_FENCE_QA
    elif lang == "relevance" and code.strip():
        examples = [
            Example(
                query="\n".join(line.rstrip() for line in code.split("\n")).strip(),
                form=FORM_FENCE_RELEVANCE,
            )
        ]
    return _Block("code", rendered, examples)


def _qna_block(lines: list[str]) -> _Block:
    examples = parse_cases(lines)
    for example in examples:
        example.form = FORM_QNA
    return _Block("code", _render_qna(lines), examples)


def _attach_context(blocks: list[_Block]) -> None:
    """Give each example the paragraphs directly before and after its block."""
    for index, block in enumerate(blocks):
        if block.kind != "code" or not block.examples:
            continue
        before = blocks[index - 1].text if index > 0 and blocks[index - 1].kind == "para" else ""
        after = (
            blocks[index + 1].text
            if index + 1 < len(blocks) and blocks[index + 1].kind == "para"
            else ""
        )
        for example in block.examples:
            example.before = _clip(before, tail=True)
            example.after = _clip(after, tail=False)


def _heading_text(raw: str) -> str:
    return " ".join(plain_text(raw).split())


def _split_sections(body: str, title: str) -> list[_Section]:
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    current = _Section(heading=title, section_path=(title,))
    # the text before the first heading is a section of its own, but an empty one
    # is not worth reporting as skipped
    in_preamble = True
    para: list[str] = []
    in_comment = False
    # fence state: (marker character, marker length, language, lines)
    fence: tuple[str, int, str, list[str]] | None = None
    qna: list[str] | None = None

    def flush_para() -> None:
        if para:
            text = plain_text("\n".join(para))
            para.clear()
            if text:
                current.blocks.append(_Block("para", text))

    def path_for() -> tuple[str, ...]:
        texts = [text for _, text in stack]
        if texts and texts[0].casefold() == title.casefold():
            texts = texts[1:]
        return (title, *texts)

    for raw in body.split("\n"):
        if fence is not None:
            marker, length, lang, buf = fence
            closing = re.match(rf"^\s*({re.escape(marker)}{{{length},}})\s*$", raw)
            if closing:
                current.blocks.append(_fence_block(lang, buf))
                fence = None
            else:
                buf.append(raw)
            continue
        if qna is not None:
            if _QNA_CLOSE_RE.match(raw):
                current.blocks.append(_qna_block(qna))
                qna = None
            else:
                qna.append(raw)
            continue

        line, in_comment = _strip_comments(raw, in_comment)
        if not line.strip():
            flush_para()
            continue
        if _QNA_OPEN_RE.match(line):
            flush_para()
            qna = []
            continue
        fence_open = _FENCE_OPEN_RE.match(line)
        if fence_open:
            flush_para()
            marker = fence_open.group(1)
            fence = (marker[0], len(marker), fence_open.group(2).lower(), [])
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush_para()
            if not (in_preamble and not current.blocks):
                sections.append(current)
            in_preamble = False
            level = len(heading.group(1))
            text = _heading_text(heading.group(2))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            current = _Section(heading=text, section_path=path_for())
            continue
        para.append(line)

    # a block left open at the end of the document is kept rather than dropped
    if fence is not None:
        current.blocks.append(_fence_block(fence[2], fence[3]))
    if qna is not None:
        current.blocks.append(_qna_block(qna))
    flush_para()
    if not (in_preamble and not current.blocks):
        sections.append(current)
    return sections


def _split_paragraph(text: str, max_chars: int) -> list[str]:
    """Split an over-long paragraph at line boundaries (a single huge line is cut)."""
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        pieces = [line[i : i + max_chars] for i in range(0, len(line), max_chars)] or [""]
        for piece in pieces:
            if current and size + len(piece) + 1 > max_chars:
                parts.append("\n".join(current))
                current, size = [], 0
            current.append(piece)
            size += len(piece) + 1
    if current:
        parts.append("\n".join(current))
    return parts


def _pack(blocks: list[_Block], max_chars: int) -> list[list[_Block]]:
    """Group blocks into parts of at most max_chars. A code block is never split."""
    expanded: list[_Block] = []
    for block in blocks:
        if block.kind == "para" and len(block.text) > max_chars:
            expanded.extend(
                _Block("para", piece) for piece in _split_paragraph(block.text, max_chars)
            )
        else:
            expanded.append(block)
    parts: list[list[_Block]] = []
    current: list[_Block] = []
    size = 0
    for block in expanded:
        added = len(block.text) + (2 if current else 0)
        if current and size + added > max_chars:
            parts.append(current)
            current, size = [block], len(block.text)
        else:
            current.append(block)
            size += added
    if current:
        parts.append(current)
    return parts


def parse_document(
    text: str,
    *,
    default_title: str,
    max_chars: int = MAX_CHUNK_CHARS,
) -> ParsedDocument:
    """Split one markdown document into chunks and examples.

    default_title names the document when it has no front-matter title (callers
    pass the file stem).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    front_title, body = split_front_matter(text)
    title = front_title or default_title
    parsed = ParsedDocument(title=title, chunks=[])
    for section in _split_sections(body, title):
        _attach_context(section.blocks)
        body_text = "\n\n".join(block.text for block in section.blocks).strip()
        if not body_text:
            parsed.empty_skipped += 1
            continue
        if _STUB_RE.match(body_text) and not any(b.kind == "code" for b in section.blocks):
            parsed.stub_skipped += 1
            continue
        parsed.leftover_templates += len(_LEFTOVER_TEMPLATE_RE.findall(section.heading)) + sum(
            len(_LEFTOVER_TEMPLATE_RE.findall(block.text))
            for block in section.blocks
            if block.kind == "para"
        )
        for part in _pack(section.blocks, max_chars):
            parsed.chunks.append(
                Chunk(
                    heading=section.heading,
                    section_path=section.section_path,
                    text="\n\n".join(block.text for block in part),
                    examples=[example for block in part for example in block.examples],
                )
            )
    return parsed

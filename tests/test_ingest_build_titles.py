"""tools/ingest/build.py: a heading-less HTML page is headed by its manifest title."""

import hashlib
import sqlite3
from datetime import UTC, datetime

from tools.ingest import build, fetch

BUILT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
FRAGMENT = b"<div><h2>Relevance</h2><p>Check the file first.</p></div>"


def url_row(tmp_path, *, source_id="wiki-tips", name="wiki-tips.html", body=FRAGMENT, **extra):
    raw = tmp_path / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / name).write_bytes(body)
    row = dict.fromkeys(fetch.MANIFEST_COLUMNS, "")
    row.update(
        id=source_id,
        path=name,
        source_url="https://example.com/" + name,
        title="Wiki: Relevance Tips",
        license="Public",
        dialect="client",
        sha256=hashlib.sha256(body).hexdigest(),
    )
    row.update(extra)
    return row


def run(tmp_path, row):
    return build.build(
        [],
        [row],
        raw_dir=tmp_path / "raw",
        md_dir=tmp_path / "md",
        index_path=tmp_path / "index" / "relevance.sqlite",
        built_at=BUILT,
    )


def headings(tmp_path):
    db = sqlite3.connect(tmp_path / "index" / "relevance.sqlite")
    try:
        return db.execute("SELECT heading, section_path FROM chunks").fetchall()
    finally:
        db.close()


class TestTitled:
    def test_a_page_with_no_top_heading_gets_its_title(self):
        text = build._titled("## Relevance\n\nBody.\n", "Wiki: Tips", ".html")
        assert text.startswith("# Wiki: Tips\n\n## Relevance")

    def test_a_page_that_has_a_top_heading_is_left_alone(self):
        text = "# Own title\n\n## Part\n\nBody.\n"
        assert build._titled(text, "Wiki: Tips", ".html") == text

    def test_a_second_level_heading_is_not_a_top_heading(self):
        assert build._titled("## Part\n", "T", ".htm").startswith("# T\n")

    def test_a_pdf_is_left_as_converted(self):
        assert build._titled("## Page 1\n", "Guide", ".pdf") == "## Page 1\n"

    def test_a_markdown_file_is_left_alone(self):
        assert build._titled("text only\n", "Guide", ".md") == "text only\n"

    def test_no_title_leaves_it_alone(self):
        assert build._titled("## Part\n", "  ", ".html") == "## Part\n"

    def test_a_hash_without_a_space_is_not_a_heading(self):
        assert build._titled("#hashtag\n", "T", ".html").startswith("# T")


class TestInTheIndex:
    def test_the_chunks_are_headed_by_the_title_not_the_file_name(self, tmp_path):
        result = run(tmp_path, url_row(tmp_path))
        assert result.wrote_index
        found = headings(tmp_path)
        assert all("wiki-tips" not in heading for heading, _ in found)
        assert any(
            heading == "Relevance" and "Wiki: Relevance Tips" in path for heading, path in found
        )

    def test_a_page_with_its_own_title_keeps_it(self, tmp_path):
        body = b"<html><head><title>Their Title</title></head><body><p>Text.</p></body></html>"
        run(tmp_path, url_row(tmp_path, body=body))
        found = headings(tmp_path)
        assert any("Their Title" in heading or "Their Title" in path for heading, path in found)
        assert not any("Wiki: Relevance Tips" in path for _, path in found)

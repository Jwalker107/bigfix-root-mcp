"""tools/ingest/build.py: end-to-end index builds over throwaway corpora."""

import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from typing import ClassVar

import pytest

from tests.ingest_git import commit_files, git, git_row, make_repo
from tests.ingest_pdf import make_pdf
from tools.ingest import build, fetch, gitsource, index

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

BUILT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
GIT_ID = "developer-bigfix-com"  # the id SOURCE_RULES labels
AWS_KEY = "".join(
    ("AK", "IA", "ABCDEFGHIJKLMNOP")
)  # assembled: no token-like literal in the source

PAGES = {
    "pages/guide/session/inspectors.md": (
        "---\ntitle: Session Inspectors\n---\n\n## Computers\n\nThe bes computers inspector.\n\n"
        "```relevance\nnames of bes computers\n```\n\nReturns every computer name.\n"
    ),
    "pages/guide/client/files.md": (
        "# Files\n\nFile inspectors.\n\n"
        '{% qna %}\nQ: exists file "x"\nA: True\nT: 1.5 ms\n{% endqna %}\n'
    ),
    "pages/guide/client/files-again.md": (
        "# Files again\n\nSame example, different page.\n\n"
        '{% qna %}\nQ: exists file "x"\nA: True\n{% endqna %}\n'
    ),
    "pages/guide/basics/strings.md": (
        '# Strings\n\nStrings are text.\n\n{% qna %}\nQ: "a" & "b"\nA: ab\n...\n{% endqna %}\n'
    ),
    "pages/_reference/docs/bit-set.md": (
        "# type: bit set\n\nA collection of bits.\n\n"
        "# bit <integer> of <bit set> : boolean\n\nReturns a bit.\n\n"
        '{% qna %}\nQ: bit 0 of bit set "10"\nA: False\nI: singular boolean\n{% endqna %}\n\n'
        "# stub : integer\n\nNo documentation exists.\n"
    ),
    "pages/index.html": "<html><body>shell</body></html>",
    "pages/.hidden/secret-notes.md": "# hidden\n\nnot indexed\n",
}


@pytest.fixture
def upstream(tmp_path):
    path = make_repo(tmp_path / "upstream")
    return path, commit_files(path, PAGES, "pages")


def checkout(tmp_path, upstream, *, source_id=GIT_ID):
    path, commit = upstream
    row = git_row(path, commit, source_id=source_id, subpath="pages")
    result = gitsource.sync_git_source(row, tmp_path / "raw", allow_schemes=("file",))
    assert result.status == fetch.STATUS_NEW, result.detail
    return row


def run(tmp_path, git_rows=(), url_rows=(), **kwargs):
    return build.build(
        list(git_rows),
        list(url_rows),
        raw_dir=tmp_path / "raw",
        md_dir=tmp_path / "md",
        index_path=tmp_path / "index" / "relevance.sqlite",
        built_at=BUILT,
        **kwargs,
    )


def rows(tmp_path, sql, *params):
    connection = sqlite3.connect(tmp_path / "index" / "relevance.sqlite")
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def url_row(
    tmp_path,
    *,
    source_id="page",
    name="page.html",
    body=b"<html><body><h1>Guide</h1><p>Text here.</p></body></html>",
    **extra,
):
    raw = tmp_path / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / name).write_bytes(body)
    row = dict.fromkeys(fetch.MANIFEST_COLUMNS, "")
    row.update(
        id=source_id,
        path=name,
        source_url="https://example.com/" + name,
        license="Public",
        bigfix_version="8.1",
        dialect="client",
        sha256=hashlib.sha256(body).hexdigest(),
    )
    row.update(extra)
    return row


class TestGitSource:
    def test_documents_chunks_and_labels(self, tmp_path, upstream):
        result = run(tmp_path, [checkout(tmp_path, upstream)])
        assert result.exit_code == build.EXIT_OK
        assert result.wrote_index
        dialects = dict(rows(tmp_path, "SELECT DISTINCT path, dialect FROM chunks"))
        assert dialects == {
            "guide/session/inspectors.md": "session",
            "guide/client/files.md": "client",
            "guide/client/files-again.md": "client",
            "guide/basics/strings.md": "both",
            "_reference/docs/bit-set.md": "unknown",
        }
        kinds = dict(rows(tmp_path, "SELECT DISTINCT path, kind FROM chunks"))
        assert kinds["_reference/docs/bit-set.md"] == "inspector"
        assert kinds["guide/client/files.md"] == "explanation"

    def test_hidden_files_and_non_markdown_are_not_indexed_but_are_reported(
        self, tmp_path, upstream
    ):
        result = run(tmp_path, [checkout(tmp_path, upstream)])
        report = result.reports[0]
        assert report.files == 5
        assert dict(report.skipped_files) == {".html": 1, "hidden": 1}
        assert not rows(
            tmp_path, "SELECT 1 FROM chunks WHERE path LIKE '%hidden%' OR path LIKE '%.html'"
        )

    def test_examples_dedupe_timing_and_flags(self, tmp_path, upstream):
        result = run(tmp_path, [checkout(tmp_path, upstream)])
        report = result.reports[0]
        assert report.duplicate_examples == 1
        by_query = {
            r[0]: r[1:]
            for r in rows(
                tmp_path,
                "SELECT query, answers, incomplete, result_type, dialect, form FROM examples",
            )
        }
        assert set(by_query) == {
            "names of bes computers",
            'exists file "x"',
            '"a" & "b"',
            'bit 0 of bit set "10"',
        }
        assert json.loads(by_query['exists file "x"'][0]) == ["True"]
        assert by_query['"a" & "b"'][1] == 1  # the `...` elision
        assert by_query['bit 0 of bit set "10"'][2] == "singular boolean"
        assert by_query["names of bes computers"][3:] == ("session", "fence-relevance")
        assert not rows(tmp_path, "SELECT 1 FROM chunks WHERE text LIKE '%1.5 ms%'")

    def test_example_context_carries_the_section_and_neighbouring_prose(self, tmp_path, upstream):
        run(tmp_path, [checkout(tmp_path, upstream)])
        (context,) = rows(
            tmp_path, "SELECT context FROM examples WHERE query = 'names of bes computers'"
        )
        assert "Session Inspectors > Computers" in context[0]
        assert "Returns every computer name." in context[0]

    def test_stub_sections_are_counted_not_indexed(self, tmp_path, upstream):
        result = run(tmp_path, [checkout(tmp_path, upstream)])
        assert result.reports[0].stub_sections == 1
        assert not rows(tmp_path, "SELECT 1 FROM chunks WHERE heading LIKE 'stub%'")
        assert (
            rows(tmp_path, "SELECT count(*) FROM chunks WHERE heading LIKE 'bit <integer>%'")[0][0]
            == 1
        )

    def test_the_source_row_records_the_pinned_commit(self, tmp_path, upstream):
        _, commit = upstream
        run(tmp_path, [checkout(tmp_path, upstream)])
        assert rows(tmp_path, "SELECT id, kind, revision, license FROM sources") == [
            (GIT_ID, "git", commit, "Apache-2.0")
        ]

    def test_a_source_without_labelling_rules_is_unknown_dialect_and_explanation_kind(
        self, tmp_path, upstream
    ):
        result = run(tmp_path, [checkout(tmp_path, upstream, source_id="other-docs")])
        assert result.exit_code == build.EXIT_OK
        assert rows(tmp_path, "SELECT DISTINCT dialect, kind FROM chunks") == [
            ("unknown", "explanation")
        ]

    def test_identical_input_gives_identical_rows(self, tmp_path, upstream):
        row = checkout(tmp_path, upstream)
        dumps = []
        for name in ("a", "b"):
            index_path = tmp_path / f"{name}.sqlite"
            build.build(
                [row],
                [],
                raw_dir=tmp_path / "raw",
                md_dir=tmp_path / "md",
                index_path=index_path,
                built_at=BUILT,
            )
            connection = sqlite3.connect(index_path)
            dumps.append(
                [
                    connection.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("sources", "chunks", "examples")
                ]
            )
            connection.close()
        assert dumps[0] == dumps[1]


class TestGitIntegrity:
    def test_a_missing_checkout_is_skipped(self, tmp_path, upstream):
        path, commit = upstream
        result = run(tmp_path, [git_row(path, commit, source_id=GIT_ID)])
        assert result.reports[0].status == build.STATUS_SKIPPED
        assert "gitsource" in result.reports[0].detail
        assert result.exit_code == build.EXIT_PROBLEM  # nothing was ingested

    def test_a_checkout_at_the_wrong_commit_is_an_error_and_writes_nothing(
        self, tmp_path, upstream
    ):
        row = checkout(tmp_path, upstream)
        newer = commit_files(upstream[0], {"pages/new.md": "# New\n\nx\n"}, "newer")
        row["commit"] = newer
        result = run(tmp_path, [row])
        assert result.reports[0].status == build.STATUS_ERROR
        assert "pinned commit" in result.reports[0].detail
        assert result.exit_code == build.EXIT_PROBLEM
        assert not result.wrote_index
        assert not (tmp_path / "index").exists()

    def test_a_modified_checkout_is_an_error(self, tmp_path, upstream):
        row = checkout(tmp_path, upstream)
        (tmp_path / "raw" / GIT_ID / "pages" / "guide" / "client" / "files.md").write_text(
            "edited", encoding="utf-8"
        )
        result = run(tmp_path, [row])
        assert result.reports[0].status == build.STATUS_ERROR
        assert "local modifications" in result.reports[0].detail
        assert not result.wrote_index

    def test_a_subpath_that_is_not_in_the_checkout_is_an_error(self, tmp_path, upstream):
        row = checkout(tmp_path, upstream)
        row["subpath"] = "nope"
        result = run(tmp_path, [row])
        assert result.reports[0].status == build.STATUS_ERROR

    def test_invalid_utf8_is_an_error_not_silent_corruption(self, tmp_path):
        path = make_repo(tmp_path / "upstream")
        commit = commit_files(path, {"pages/ok.md": "# ok\n\nfine\n"})
        (path / "pages" / "bad.md").write_bytes(b"# bad\n\n\xff\xfe broken\n")
        git(path, "add", "--all")
        git(path, "commit", "--quiet", "--message", "bad")
        commit = git(path, "rev-parse", "HEAD")
        row = git_row(path, commit, source_id=GIT_ID)
        gitsource.sync_git_source(row, tmp_path / "raw", allow_schemes=("file",))
        result = run(tmp_path, [row])
        assert result.reports[0].status == build.STATUS_ERROR
        assert "UTF-8" in result.reports[0].detail


class TestUrlSources:
    def test_html_is_converted_and_indexed(self, tmp_path):
        result = run(tmp_path, url_rows=[url_row(tmp_path)])
        assert result.exit_code == build.EXIT_OK
        assert (tmp_path / "md" / "page.md").read_text(encoding="utf-8").startswith("# Guide")
        (chunk,) = rows(tmp_path, "SELECT path, heading, dialect, version, kind FROM chunks")
        assert chunk == ("page.md", "Guide", "client", "8.1", "explanation")
        body = hashlib.sha256(
            b"<html><body><h1>Guide</h1><p>Text here.</p></body></html>"
        ).hexdigest()
        assert rows(tmp_path, "SELECT kind, revision, version FROM sources") == [
            ("url", body, "8.1")
        ]

    def test_a_source_never_fetched_is_skipped_and_the_rest_still_builds(self, tmp_path, upstream):
        git_source = checkout(tmp_path, upstream)
        unfetched = dict.fromkeys(fetch.MANIFEST_COLUMNS, "")
        unfetched.update(id="later", source_url="https://example.com/later.pdf")
        result = run(tmp_path, [git_source], [unfetched])
        assert result.exit_code == build.EXIT_OK
        assert [r.status for r in result.reports] == [build.STATUS_INGESTED, build.STATUS_SKIPPED]
        assert "fetch" in result.reports[1].detail
        assert result.wrote_index

    def test_strict_turns_a_skipped_source_into_a_failure(self, tmp_path, upstream):
        git_source = checkout(tmp_path, upstream)
        unfetched = dict.fromkeys(fetch.MANIFEST_COLUMNS, "")
        unfetched.update(id="later", source_url="https://example.com/later.pdf")
        result = run(tmp_path, [git_source], [unfetched], strict=True)
        assert result.exit_code == build.EXIT_PROBLEM
        assert not result.wrote_index

    def test_a_raw_file_that_does_not_match_the_manifest_hash_is_an_error(self, tmp_path):
        row = url_row(tmp_path)
        (tmp_path / "raw" / "page.html").write_bytes(b"<html><body><p>tampered</p></body></html>")
        result = run(tmp_path, url_rows=[row])
        assert result.reports[0].status == build.STATUS_ERROR
        assert "sha256" in result.reports[0].detail
        assert not result.wrote_index

    def test_a_missing_raw_file_is_skipped(self, tmp_path):
        row = url_row(tmp_path)
        (tmp_path / "raw" / "page.html").unlink()
        result = run(tmp_path, url_rows=[row])
        assert result.reports[0].status == build.STATUS_SKIPPED
        assert "missing" in result.reports[0].detail

    def test_a_pdf_is_skipped_when_pypdf_is_not_installed(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "pypdf", None)
        row = url_row(tmp_path, source_id="guide", name="guide.pdf", body=b"%PDF-1.4 fake")
        result = run(tmp_path, url_rows=[row])
        assert result.reports[0].status == build.STATUS_SKIPPED
        assert "pypdf" in result.reports[0].detail

    def test_a_real_pdf_is_extracted_and_indexed(self, tmp_path):
        pytest.importorskip("pypdf")
        body = make_pdf(["Core inspectors reference", "Registry keys and values"])
        row = url_row(tmp_path, source_id="guide", name="guide.pdf", body=body)
        result = run(tmp_path, url_rows=[row])
        assert result.exit_code == build.EXIT_OK
        assert rows(tmp_path, "SELECT heading, text FROM chunks ORDER BY id") == [
            ("Page 1", "Core inspectors reference"),
            ("Page 2", "Registry keys and values"),
        ]
        converted = (tmp_path / "md" / "guide.md").read_text(encoding="utf-8")
        assert converted.startswith("## Page 1")

    def test_a_corrupt_pdf_is_a_source_error_not_a_crash(self, tmp_path):
        pytest.importorskip("pypdf")
        row = url_row(tmp_path, source_id="bad", name="bad.pdf", body=b"%PDF-1.4 not really a pdf")
        result = run(tmp_path, url_rows=[row])
        assert result.reports[0].status == build.STATUS_ERROR
        assert "conversion failed" in result.reports[0].detail
        assert not result.wrote_index

    def test_a_pdf_with_no_text_layer_is_an_error(self, tmp_path):
        pytest.importorskip("pypdf")
        row = url_row(tmp_path, source_id="blank", name="blank.pdf", body=make_pdf(["", ""]))
        result = run(tmp_path, url_rows=[row])
        assert result.reports[0].status == build.STATUS_ERROR
        assert "no text" in result.reports[0].detail

    def test_a_file_type_with_no_converter_is_skipped(self, tmp_path):
        row = url_row(tmp_path, name="data.xlsx", body=b"PK fake")
        result = run(tmp_path, url_rows=[row])
        assert result.reports[0].status == build.STATUS_SKIPPED
        assert "no converter" in result.reports[0].detail

    def test_an_invalid_dialect_is_an_error(self, tmp_path):
        result = run(tmp_path, url_rows=[url_row(tmp_path, dialect="martian")])
        assert result.reports[0].status == build.STATUS_ERROR
        assert "dialect" in result.reports[0].detail

    def test_a_blank_dialect_means_unknown(self, tmp_path):
        run(tmp_path, url_rows=[url_row(tmp_path, dialect="")])
        assert rows(tmp_path, "SELECT DISTINCT dialect FROM chunks") == [("unknown",)]

    def test_conversion_that_produces_no_text_is_an_error(self, tmp_path):
        row = url_row(tmp_path, body=b"<html><body><script>x</script></body></html>")
        result = run(tmp_path, url_rows=[row])
        assert result.reports[0].status == build.STATUS_ERROR
        assert "no text" in result.reports[0].detail


class TestSecrets:
    def secret_upstream(self, tmp_path, text):
        path = make_repo(tmp_path / "upstream")
        commit = commit_files(path, {"pages/a.md": text})
        row = git_row(path, commit, source_id=GIT_ID)
        gitsource.sync_git_source(row, tmp_path / "raw", allow_schemes=("file",))
        return row

    def test_a_blocking_finding_stops_the_build_and_the_report_never_shows_the_secret(
        self, tmp_path
    ):
        row = self.secret_upstream(tmp_path, f"# A\n\nkey is {AWS_KEY}\n")
        result = run(tmp_path, [row])
        assert result.exit_code == build.EXIT_SECRET
        assert not result.wrote_index
        assert not (tmp_path / "index").exists()
        report = build.format_report(result, tmp_path / "index" / "relevance.sqlite", dry_run=False)
        assert "BLOCK aws-access-key" in report
        assert "a.md:3" in report
        assert AWS_KEY not in report
        assert "index NOT written" in report

    def test_the_allowlist_can_suppress_a_known_false_positive(self, tmp_path):
        row = self.secret_upstream(tmp_path, f"# A\n\nkey is {AWS_KEY}\n")
        result = run(tmp_path, [row], allowed={(GIT_ID, "a.md", "aws-access-key")})
        assert result.exit_code == build.EXIT_OK

    def test_a_warning_is_reported_but_does_not_stop_the_build(self, tmp_path):
        row = self.secret_upstream(tmp_path, "# A\n\nAsk jane.doe@corp.test.io about it.\n")
        result = run(tmp_path, [row])
        assert result.exit_code == build.EXIT_OK
        report = build.format_report(result, tmp_path / "index" / "relevance.sqlite", dry_run=False)
        assert "warn  email-address" in report
        assert "jane.doe" not in report


class TestModes:
    def test_dry_run_writes_neither_the_index_nor_converted_markdown(self, tmp_path, upstream):
        result = run(tmp_path, [checkout(tmp_path, upstream)], [url_row(tmp_path)], dry_run=True)
        assert result.exit_code == build.EXIT_OK
        assert not result.wrote_index
        assert not (tmp_path / "index").exists()
        assert not (tmp_path / "md").exists()
        assert result.data.chunks
        report = build.format_report(result, tmp_path / "index" / "relevance.sqlite", dry_run=True)
        assert "dry run: would write" in report

    def test_only_limits_the_sources(self, tmp_path, upstream):
        git_source = checkout(tmp_path, upstream)
        result = run(tmp_path, [git_source], [url_row(tmp_path)], dry_run=True, only=["page"])
        assert [r.id for r in result.reports] == ["page"]

    def test_no_sources_is_not_a_successful_build(self, tmp_path):
        result = run(tmp_path)
        assert result.exit_code == build.EXIT_PROBLEM
        assert not result.wrote_index

    def test_the_report_summarises_each_source(self, tmp_path, upstream):
        result = run(tmp_path, [checkout(tmp_path, upstream)])
        report = build.format_report(result, tmp_path / "index" / "relevance.sqlite", dry_run=False)
        assert "ingested" in report
        assert "duplicate-examples=1" in report
        assert "stub-sections=1" in report
        assert "template-leftovers=0" in report
        assert "files not indexed: .html=1, hidden=1" in report
        assert "index written to" in report

    def test_unrecognised_template_syntax_is_counted_and_reported(self, tmp_path):
        path = make_repo(tmp_path / "upstream")
        commit = commit_files(path, {"pages/a.md": "# A\n\nUses {{ unknown | filter }} here.\n"})
        row = git_row(path, commit, source_id=GIT_ID)
        gitsource.sync_git_source(row, tmp_path / "raw", allow_schemes=("file",))
        result = run(tmp_path, [row])
        assert result.exit_code == build.EXIT_OK  # visible, not fatal
        assert result.reports[0].leftover_templates == 1
        report = build.format_report(result, tmp_path / "index" / "relevance.sqlite", dry_run=False)
        assert "template-leftovers=1" in report


class TestLabelling:
    rules = build.SOURCE_RULES[GIT_ID]

    @pytest.mark.parametrize(
        ("path", "dialect", "kind"),
        [
            ("guide/session/computerinspectors.md", "session", "explanation"),
            ("guide/client/files.md", "client", "explanation"),
            ("tutorial/client/index.md", "client", "explanation"),
            ("guide/basics/strings.md", "both", "explanation"),
            ("tutorial.md", "both", "explanation"),
            ("_reference/docs/bit-set.md", "unknown", "inspector"),
            ("_reference/other.md", "unknown", "explanation"),
        ],
    )
    def test_developer_site_rules(self, path, dialect, kind):
        assert build.dialect_for(self.rules, path) == dialect
        assert build.kind_for(self.rules, path) == kind


class TestMain:
    def write_manifests(self, tmp_path, git_row_=None, url_rows=()):
        git_path = tmp_path / "GIT_SOURCES.csv"
        lines = [",".join(gitsource.GIT_COLUMNS)]
        if git_row_:
            lines.append(",".join(git_row_[name] for name in gitsource.GIT_COLUMNS))
        git_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest = tmp_path / "MANIFEST.csv"
        out = [",".join(fetch.MANIFEST_COLUMNS)]
        out += [",".join(row[name] for name in fetch.MANIFEST_COLUMNS) for row in url_rows]
        manifest.write_text("\n".join(out) + "\n", encoding="utf-8")
        return [
            "--git-sources", str(git_path),
            "--manifest", str(manifest),
            "--scan-allow", str(tmp_path / "none.csv"),
            "--local-sources", str(tmp_path / "local-sources.csv"),
            "--raw-dir", str(tmp_path / "raw"),
            "--md-dir", str(tmp_path / "md"),
            "--index", str(tmp_path / "index" / "relevance.sqlite"),
        ]  # fmt: skip

    def test_end_to_end(self, tmp_path, upstream, capsys):
        args = self.write_manifests(tmp_path, checkout(tmp_path, upstream))
        assert build.main(args) == build.EXIT_OK
        assert "index written to" in capsys.readouterr().out
        assert (tmp_path / "index" / "relevance.sqlite").is_file()

    def test_only_requires_dry_run(self, tmp_path, upstream):
        args = self.write_manifests(tmp_path, checkout(tmp_path, upstream))
        assert build.main([*args, "--only", GIT_ID]) == build.EXIT_USAGE
        assert not (tmp_path / "index").exists()
        assert build.main([*args, "--only", GIT_ID, "--dry-run"]) == build.EXIT_OK

    def test_a_tiny_chunk_size_is_rejected(self, tmp_path):
        args = self.write_manifests(tmp_path)
        assert build.main([*args, "--max-chunk-chars", "10"]) == build.EXIT_USAGE

    def test_an_unreadable_manifest_exits_2(self, tmp_path):
        args = self.write_manifests(tmp_path)
        (tmp_path / "MANIFEST.csv").unlink()
        assert build.main(args) == build.EXIT_USAGE

    def test_an_id_used_in_both_manifests_exits_2(self, tmp_path, upstream):
        row = checkout(tmp_path, upstream, source_id="same")
        clash = url_row(tmp_path, source_id="same")
        assert build.main(self.write_manifests(tmp_path, row, [clash])) == build.EXIT_USAGE

    def test_a_missing_git_sources_file_is_fine(self, tmp_path, capsys):
        args = self.write_manifests(tmp_path, url_rows=[url_row(tmp_path)])
        args[1] = str(tmp_path / "no-such-git-sources.csv")
        assert build.main(args) == build.EXIT_OK


def file_row(location, *, source_id="private-doc", notes="private; legal sign-off"):
    return {"id": source_id, "kind": "file", "location": str(location), "notes": notes}


class TestPrivateFiles:
    """A `file` row of the private list: one document, read in place, never pinned."""

    def test_a_markdown_file_is_indexed_with_its_hash_and_only_its_name(self, tmp_path):
        doc = tmp_path / "private" / "Upgrade Steps.md"
        doc.parent.mkdir()
        doc.write_text("## Steps\n\nStop the service, then replace the binary.\n", encoding="utf-8")
        result = run(tmp_path, file_rows=[file_row(doc)])
        assert result.exit_code == build.EXIT_OK, result.reports
        assert result.reports[0].kind == "file"
        [(kind, origin, revision, license_)] = rows(
            tmp_path, "SELECT kind, origin, revision, license FROM sources"
        )
        assert kind == "file"
        assert origin == "private file Upgrade Steps.md"
        assert str(tmp_path) not in origin  # the full path stays out of the index
        assert revision == hashlib.sha256(doc.read_bytes()).hexdigest()
        assert license_ == "private; legal sign-off"
        headings = [h for (h,) in rows(tmp_path, "SELECT section_path FROM chunks")]
        assert any("Upgrade Steps" in h for h in headings)  # the file name heads it

    def test_a_pdf_is_converted(self, tmp_path):
        pytest.importorskip("pypdf")
        doc = tmp_path / "guide.pdf"
        doc.write_bytes(make_pdf(["Stop the BES WebUI service before you replace node."]))
        result = run(tmp_path, file_rows=[file_row(doc)])
        assert result.exit_code == build.EXIT_OK, result.reports
        texts = " ".join(t for (t,) in rows(tmp_path, "SELECT text FROM chunks"))
        assert "BES WebUI service" in texts

    def test_a_missing_file_is_skipped_not_fatal(self, tmp_path):
        result = run(tmp_path, file_rows=[file_row(tmp_path / "gone.pdf")])
        assert result.reports[0].status == build.STATUS_SKIPPED
        assert "gone.pdf" in result.reports[0].detail

    def test_main_reads_file_rows_from_the_private_list(self, tmp_path, capsys):
        doc = tmp_path / "notes.md"
        doc.write_text("# Notes\n\nA private page.\n", encoding="utf-8")
        args = TestMain().write_manifests(tmp_path)
        (tmp_path / "local-sources.csv").write_text(
            "id,kind,location,branch,commit,subpath,group,notes\n"
            f"team-notes,file,{doc},,,,team,private\n"
            f"team-fixlets,dir,{tmp_path},,,,team,a fixlet folder is not a document\n",
            encoding="utf-8",
        )
        assert build.main(args) == build.EXIT_OK
        ids = [i for (i,) in rows(tmp_path, "SELECT id FROM sources")]
        assert ids == ["team-notes"]


class TestAnalyzerVerdict:
    """The offline analyzer's verdict is stored with each example (schema version 2)."""

    FILES: ClassVar[dict[str, str]] = {
        "pages/_reference/docs/thing.md": (
            "# thing\n\nText.\n\n```\n"
            "Q: names of bes computers\nA: PC1\n\n"
            'Q: exists file "x" whose (nonsense of it)\nA: True\n\n'
            'Q: bit 0 of bit set "10"\nA: False\n\n'
            "Q: number of of bes computers\nE: boom\n"
            "```\n"
        ),
        "pages/guide/session/s.md": (
            "# s\n\nText.\n\n```relevance\nlinks (name of it)) of bes computers\n```\n"
        ),
        "pages/guide/client/c.md": (
            "# c\n\nText.\n\n{% qna %}\nQ: number of bes computers\nA: 3\n{% endqna %}\n"
        ),
    }

    @pytest.fixture
    def built(self, tmp_path):
        path = make_repo(tmp_path / "upstream")
        commit = commit_files(path, self.FILES)
        row = git_row(path, commit, source_id=GIT_ID)
        gitsource.sync_git_source(row, tmp_path / "raw", allow_schemes=("file",))
        result = run(tmp_path, [row])
        assert result.exit_code == build.EXIT_OK
        return tmp_path, row, result

    def by_query(self, tmp_path):
        found = rows(
            tmp_path,
            "SELECT query, dialect, dialect_source, parses, unknown_inspectors, parse_error"
            " FROM examples",
        )
        return {query: rest for query, *rest in found}

    def test_an_unknown_dialect_is_labelled_only_when_the_analyzer_is_definite(self, built):
        examples = self.by_query(built[0])
        assert examples["names of bes computers"][:3] == ["session", "analyzer", 1]
        assert examples['exists file "x" whose (nonsense of it)'][:3] == ["client", "analyzer", 1]

    def test_a_query_valid_in_both_dialects_stays_unknown(self, built):
        # the analyzer resolves it as 'both': not evidence for either, so no label
        assert self.by_query(built[0])['bit 0 of bit set "10"'][:3] == ["unknown", "source", 1]

    def test_a_query_that_does_not_parse_is_never_labelled(self, built):
        # the text looks like session relevance, but a broken query is not evidence
        found = self.by_query(built[0])["number of of bes computers"]
        assert found[:3] == ["unknown", "source", 0]
        assert found[4]  # the parse error is kept

    def test_a_stated_dialect_is_never_overridden(self, built):
        examples = self.by_query(built[0])
        # a session-looking query under guide/client stays client
        assert examples["number of bes computers"][:3] == ["client", "source", 1]
        # a broken query under guide/session stays session, and is flagged
        broken = examples["links (name of it)) of bes computers"]
        assert broken[:3] == ["session", "source", 0]
        assert "unexpected" in broken[4]

    def test_unknown_inspectors_are_stored(self, built):
        examples = self.by_query(built[0])
        assert json.loads(examples['exists file "x" whose (nonsense of it)'][3]) == ["nonsense"]
        assert json.loads(examples["names of bes computers"][3]) == []

    def test_the_report_counts_labelled_and_unparsed_examples(self, built):
        tmp_path, _, result = built
        assert result.reports[0].examples_relabelled == 2
        assert result.reports[0].examples_unparsed == 2
        report = build.format_report(result, tmp_path / "index" / "relevance.sqlite", dry_run=False)
        assert "examples-unparsed=2" in report
        assert "dialect-labelled=2" in report

    def test_the_index_records_which_analyzer_judged_the_examples(self, built):
        from tools.ingest import verdict

        meta = dict(rows(built[0], "SELECT key, value FROM meta"))
        assert meta["analyzer_version"] == verdict.analyzer_version()
        assert meta["schema_version"] == str(index.SCHEMA_VERSION)

    def test_an_analyzer_crash_leaves_examples_unjudged_not_the_build_failed(
        self, tmp_path, monkeypatch
    ):
        from tools.ingest import verdict

        def boom(*_args, **_kwargs):
            raise RuntimeError("analyzer bug")

        monkeypatch.setattr(verdict, "analyze_relevance", boom)
        path = make_repo(tmp_path / "upstream")
        commit = commit_files(path, self.FILES)
        row = git_row(path, commit, source_id=GIT_ID)
        gitsource.sync_git_source(row, tmp_path / "raw", allow_schemes=("file",))
        result = run(tmp_path, [row])
        assert result.exit_code == build.EXIT_OK
        found = rows(tmp_path, "SELECT parses, parse_error, dialect, dialect_source FROM examples")
        assert found
        assert all(parses is None for parses, *_ in found)
        assert all("analyzer bug" in error for _, error, *_ in found)
        assert {dialect for *_, dialect, _source in found} <= {"unknown", "session", "client"}
        assert all(source == "source" for *_, source in found)  # nothing was labelled
        assert result.reports[0].examples_unparsed == 0  # an unjudged example is not "unparsed"

    def test_the_same_query_under_the_same_pin_is_analysed_once(self, tmp_path, monkeypatch):
        from tools.ingest import verdict

        calls = []
        real = verdict.analyze_relevance

        def spy(text, **kwargs):
            calls.append(text)
            return real(text, **kwargs)

        monkeypatch.setattr(verdict, "analyze_relevance", spy)
        page = "# p\n\nText.\n\n{% qna %}\nQ: number of bes computers\nA: 3\n{% endqna %}\n"
        path = make_repo(tmp_path / "upstream")
        commit = commit_files(
            path, {"pages/guide/client/a.md": page, "pages/guide/client/b.md": page}
        )
        row = git_row(path, commit, source_id=GIT_ID)
        gitsource.sync_git_source(row, tmp_path / "raw", allow_schemes=("file",))
        run(tmp_path, [row])
        assert calls == ["number of bes computers"]

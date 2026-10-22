"""tools/eval/analyze_examples.py: the offline analyzer pass over index examples."""

import csv
import hashlib

import pytest

from tools.eval import analyze_examples as ae
from tools.ingest import index


def row(
    query,
    dialect="unknown",
    *,
    errors=False,
    form="qna",
    incomplete=False,
    example_id=1,
    path="p.md",
):
    return ae.ExampleRow(example_id, "s", path, dialect, form, query, errors, incomplete)


def result(
    *,
    dialect="unknown",
    parsed=True,
    documents_error=False,
    pinned="",
    effective="client",
    unknown=(),
    findings=(),
    mismatch=False,
    crashed="",
    parse_error="",
    incomplete=False,
):
    return ae.Result(
        row=row("q", dialect, errors=documents_error, incomplete=incomplete),
        pinned=pinned,
        parsed=parsed,
        parse_error=parse_error,
        effective=effective,
        mismatch=mismatch,
        findings=list(findings),
        unknown_inspectors=list(unknown),
        crashed=crashed,
    )


class TestAnalyzeExample:
    def test_a_clean_client_example(self):
        found = ae.analyze_example(row('exists file "x"', "client"))
        assert found.parsed is True
        assert found.pinned == "client"
        assert found.effective == "client"
        assert found.findings == []
        assert found.unknown_inspectors == []
        assert found.mismatch is False
        assert found.crashed == ""

    def test_a_parse_failure_is_recorded_with_its_message(self):
        found = ae.analyze_example(row("number of of bes computers"))
        assert found.parsed is False
        assert found.parse_error
        assert "parse-error" in {code for code, _ in found.findings}

    def test_an_unknown_inspector_is_listed_and_the_example_still_parses(self):
        found = ae.analyze_example(row('exists file "x" whose (nonsense of it)'))
        assert found.parsed is True
        assert found.unknown_inspectors == ["nonsense"]
        assert "unknown-inspector" in {code for code, _ in found.findings}

    def test_a_client_query_pinned_as_session_is_a_mismatch(self):
        found = ae.analyze_example(row('exists file "x"', "session"))
        assert found.pinned == "session"
        assert found.mismatch is True

    def test_a_session_query_pinned_as_client_is_a_mismatch(self):
        found = ae.analyze_example(row("number of bes computers", "client"))
        assert found.mismatch is True

    def test_a_query_that_agrees_with_its_pin_is_not_a_mismatch(self):
        assert ae.analyze_example(row("number of bes computers", "session")).mismatch is False

    def test_unpinned_examples_are_classified_by_the_analyzer(self):
        assert ae.analyze_example(row("number of bes computers")).effective == "session"
        assert ae.analyze_example(row('exists file "x"')).effective == "client"

    @pytest.mark.parametrize("dialect", ["both", "unknown"])
    def test_dialects_that_are_not_session_or_client_are_not_pinned(self, dialect):
        found = ae.analyze_example(row("number of bes computers", dialect))
        assert found.pinned == ""
        assert found.mismatch is False  # nothing to contradict

    def test_an_analyzer_crash_is_contained_and_recorded(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("analyzer bug")

        monkeypatch.setattr(ae, "analyze_relevance", boom)
        found = ae.analyze_example(row("anything"))
        assert found.parsed is False
        assert "analyzer bug" in found.crashed
        assert found.row.query == "anything"

    def test_one_crash_does_not_stop_the_rest(self, monkeypatch):
        real = ae.analyze_relevance
        monkeypatch.setattr(
            ae,
            "analyze_relevance",
            lambda text, **kw: (
                (_ for _ in ()).throw(ValueError("x")) if text == "bad" else real(text, **kw)
            ),
        )
        found = ae.analyze_all([row("bad", example_id=1), row('exists file "x"', example_id=2)])
        assert [bool(r.crashed) for r in found] == [True, False]
        assert found[1].parsed is True


class TestSummarize:
    def results(self):
        return [
            result(dialect="client", pinned="client", findings=[("non-unique-risk", "warning")]),
            result(dialect="session", pinned="session", parsed=False, documents_error=True,
                   parse_error="boom", effective="session"),
            result(dialect="session", pinned="session", parsed=False, documents_error=False,
                   parse_error="oops", effective="session"),
            result(dialect="unknown", unknown=["idp user", "idp groups"],
                   findings=[("unknown-inspector", "warning"), ("non-unique-risk", "warning")]),
            result(dialect="unknown", unknown=["idp user"],
                   findings=[("unknown-inspector", "warning")]),
            result(dialect="session", pinned="session", mismatch=True, effective="session"),
            result(dialect="unknown", effective="session"),
            result(dialect="both", effective="client", incomplete=True),
            result(dialect="client", pinned="client", crashed="RuntimeError: x", parsed=False),
        ]  # fmt: skip

    def test_headline_counts(self):
        summary = ae.summarize(self.results())
        assert summary["total"] == 9
        assert summary["parsed"] == 6
        assert summary["parse_failed"] == 2  # a crash is not a parse failure
        assert summary["parse_failed_documented_error"] == 1
        assert summary["parse_failed_unexpected"] == 1
        assert summary["crashed"] == 1
        assert summary["dialect_mismatch"] == 1
        assert summary["incomplete_examples"] == 1

    def test_unknown_inspectors_are_counted_per_example_not_per_occurrence(self):
        summary = ae.summarize(self.results())
        assert summary["with_unknown_inspectors"] == 2
        assert summary["distinct_unknown_inspectors"] == 2
        assert summary["top_unknown_inspectors"] == [("idp user", 2), ("idp groups", 1)]

    def test_finding_codes_count_examples_not_findings(self):
        counts = dict(ae.summarize(self.results())["finding_codes"])
        assert counts == {"non-unique-risk": 2, "unknown-inspector": 2}

    def test_breakdown_by_the_dialect_the_source_states(self):
        groups = ae.summarize(self.results())["by_source_dialect"]
        assert groups["session"] == {
            "total": 3, "parsed": 1, "with_unknown_inspectors": 0, "mismatch": 1
        }  # fmt: skip
        assert groups["unknown"]["with_unknown_inspectors"] == 2
        assert set(groups) == {"both", "client", "session", "unknown"}

    def test_only_parsed_unpinned_examples_are_classified(self):
        classified = ae.summarize(self.results())["unpinned_classification"]
        assert classified == {"client": 3, "session": 1}

    def test_unexpected_failures_are_listed_with_their_error(self):
        listed = ae.summarize(self.results())["unexpected_failures"]
        assert listed == [("p.md", "q", "oops")]

    def test_an_empty_run_does_not_divide_by_zero(self):
        summary = ae.summarize([])
        assert summary["total"] == 0
        text = ae.format_report(summary)
        assert "examples analysed: 0" in text
        assert "n/a" in text


class TestReport:
    def test_the_report_names_each_measurement(self):
        text = ae.format_report(ae.summarize(TestSummarize().results()))
        for expected in (
            "examples analysed: 9",
            "parsed cleanly:   6",
            "document an error themselves (expected): 1",
            "document no error (unexpected):          1",
            "analyzer crashed: 1",
            "2 distinct names",
            "contradict their pinned dialect: 1",
            "by the dialect the source states:",
            "how the analyzer classifies examples whose dialect the source does not pin:",
            "2  idp user",
            "p.md: 'q' -> oops",
        ):
            assert expected in text, expected

    def test_percentages_are_shown(self):
        text = ae.format_report(ae.summarize([result(), result(parsed=False)]))
        assert "(50.0%)" in text


class TestIndexAndCsv:
    @pytest.fixture
    def index_path(self, tmp_path):
        data = index.IndexData(sources=[index.SourceRow("s1", "git", "https://x/r.git", "a" * 40)])
        data.chunks = [
            index.ChunkRow(
                "s1", "a.md", "A", ("doc", "A"), "explanation", "client", "", "text",
                examples=[
                    index.ExampleRow("qna", 'exists file "x"', ("True",), (), "", False, "c"),
                    index.ExampleRow(
                        "qna", "number of of bes computers", (), ("bad",), "", True, "c"
                    ),
                    index.ExampleRow(
                        "fence-relevance", "line one\nline two", (), (), "", False, "c"
                    ),
                ],
            )
        ]  # fmt: skip
        path = tmp_path / "relevance.sqlite"
        index.write_index(path, data)
        return path

    def test_examples_are_loaded_with_their_flags(self, index_path):
        rows = ae.load_examples(index_path)
        assert [r.query for r in rows] == [
            'exists file "x"',
            "number of of bes computers",
            "line one\nline two",
        ]
        assert [r.documents_error for r in rows] == [False, True, False]
        assert [r.incomplete for r in rows] == [False, True, False]
        assert rows[0].dialect == "client"
        assert rows[0].source_id == "s1"

    def test_the_index_is_only_read(self, index_path):
        before = hashlib.sha256(index_path.read_bytes()).hexdigest()
        files = sorted(p.name for p in index_path.parent.iterdir())
        ae.analyze_all(ae.load_examples(index_path))
        assert hashlib.sha256(index_path.read_bytes()).hexdigest() == before
        assert sorted(p.name for p in index_path.parent.iterdir()) == files

    def test_main_prints_the_report_and_writes_the_csv(self, index_path, tmp_path, capsys):
        target = tmp_path / "out.csv"
        assert ae.main(["--index", str(index_path), "--csv", str(target)]) == 0
        output = capsys.readouterr().out
        assert "examples analysed: 3" in output
        assert f"per-example results written to {target}" in output
        with target.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 3
        assert rows[1]["documents_error"] == "1"
        assert rows[1]["parsed"] == "0"
        assert rows[2]["query"] == "line one\nline two"  # the csv module quotes the newline

    def test_long_queries_are_cut_in_the_csv(self, tmp_path):
        long_row = row("number of bes computers " + "x" * 500)
        target = tmp_path / "out.csv"
        ae.write_csv([ae.analyze_example(long_row)], target)
        with target.open(newline="", encoding="utf-8") as handle:
            (written,) = list(csv.DictReader(handle))
        assert len(written["query"]) == ae.CSV_QUERY_CHARS

    def test_a_missing_index_is_a_clean_exit_2(self, tmp_path, capsys):
        assert ae.main(["--index", str(tmp_path / "nope.sqlite")]) == 2
        assert "examples analysed" not in capsys.readouterr().out

    def test_a_file_that_is_not_an_index_is_a_clean_exit_2(self, tmp_path):
        junk = tmp_path / "junk.sqlite"
        junk.write_bytes(b"not a database " * 50)
        assert ae.main(["--index", str(junk)]) == 2

    def test_unprintable_characters_do_not_break_the_report(self, tmp_path, capsys):
        data = index.IndexData(sources=[index.SourceRow("s1", "git", "x", "y")])
        odd = "exists file \u201cx\u201d"  # curly quotes: the console may not print them
        data.chunks = [
            index.ChunkRow(
                "s1", "a.md", "A", ("A",), "explanation", "client", "", "t",
                examples=[index.ExampleRow("qna", odd, (), (), "", False, "c")],
            )
        ]  # fmt: skip
        path = tmp_path / "relevance.sqlite"
        index.write_index(path, data)
        assert ae.main(["--index", str(path)]) == 0

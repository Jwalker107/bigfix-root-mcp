"""tools/eval/golden.py: selecting the golden set of examples from the index."""

import hashlib
import json
import sqlite3
from collections import Counter

import pytest

from tools.eval import golden
from tools.ingest import index


def candidate(**overrides):
    fields = {
        "example_id": 1,
        "source_id": "s1",
        "path": "p.md",
        "dialect": "client",
        "dialect_source": "source",
        "form": "qna",
        "query": 'exists file "x"',
        "answers": ("True",),
        "errors": (),
        "result_type": "",
        "incomplete": False,
        "parses": True,
        "unknown_inspectors": (),
    }
    fields.update(overrides)
    return golden.Candidate(**fields)


class TestExclusionReason:
    def test_a_documented_parsing_answer_qualifies(self):
        assert golden.exclusion_reason(candidate()) is None

    def test_a_fenced_qa_example_qualifies_too(self):
        assert golden.exclusion_reason(candidate(form="fence-qa")) is None

    def test_a_bare_fence_is_excluded_because_it_has_no_expected_output(self):
        assert (
            golden.exclusion_reason(candidate(form="fence-relevance", answers=())) == "bare-fence"
        )

    def test_an_example_the_analyzer_never_judged_is_excluded(self):
        assert golden.exclusion_reason(candidate(parses=None)) == "not-analysed"

    def test_an_example_that_does_not_parse_is_excluded(self):
        assert golden.exclusion_reason(candidate(parses=False)) == "does-not-parse"

    def test_an_incomplete_example_is_excluded(self):
        assert golden.exclusion_reason(candidate(incomplete=True)) == "incomplete"

    def test_an_example_that_documents_an_error_is_excluded(self):
        assert golden.exclusion_reason(candidate(errors=("boom",), answers=())) == "documents-error"

    def test_an_example_with_no_answer_is_excluded(self):
        assert golden.exclusion_reason(candidate(answers=())) == "no-documented-answer"

    def test_an_unknown_inspector_does_not_exclude(self):
        # as likely to be newer documentation as a mistake, so it is kept and flagged
        assert golden.exclusion_reason(candidate(unknown_inspectors=("idp user",))) is None

    def test_the_first_failing_rule_wins_in_the_documented_order(self):
        everything_wrong = candidate(
            form="fence-relevance", parses=False, incomplete=True, errors=("e",), answers=()
        )
        assert golden.exclusion_reason(everything_wrong) == "bare-fence"
        assert golden.exclusion_reason(candidate(parses=False, incomplete=True)) == "does-not-parse"
        assert golden.exclusion_reason(candidate(incomplete=True, errors=("e",))) == "incomplete"
        assert golden.exclusion_reason(candidate(errors=("e",), answers=())) == "documents-error"

    def test_every_reason_is_reachable_and_listed(self):
        reasons = {
            golden.exclusion_reason(c)
            for c in (
                candidate(form="fence-relevance"),
                candidate(parses=None),
                candidate(parses=False),
                candidate(incomplete=True),
                candidate(errors=("e",)),
                candidate(answers=()),
            )
        }
        assert reasons == set(golden.REASONS)


class TestSelect:
    def test_selected_examples_keep_index_order_and_exclusions_are_counted(self):
        examples = [
            candidate(example_id=1),
            candidate(example_id=2, parses=False),
            candidate(example_id=3),
            candidate(example_id=4, answers=()),
            candidate(example_id=5, parses=False),
        ]
        selection = golden.select(examples)
        assert [e.example_id for e in selection.selected] == [1, 3]
        assert selection.excluded == Counter({"does-not-parse": 2, "no-documented-answer": 1})

    def test_summary_breaks_the_set_down(self):
        selection = golden.select(
            [
                candidate(example_id=1, dialect="client"),
                candidate(example_id=2, dialect="session", dialect_source="analyzer"),
                candidate(example_id=3, dialect="session", unknown_inspectors=("x",)),
                candidate(example_id=4, parses=False),
            ]
        )
        summary = golden.summarize(selection, total=4)
        assert summary["golden"] == 3
        assert summary["examples_in_index"] == 4
        assert summary["by_dialect"] == {"client": 1, "session": 2}
        assert summary["by_dialect_source"] == {"source": 2, "analyzer": 1}
        assert summary["with_unknown_inspectors"] == 1
        assert summary["excluded"]["does-not-parse"] == 1
        assert set(summary["excluded"]) == set(golden.REASONS)  # every reason, even at zero

    def test_an_empty_index_gives_an_empty_set(self):
        selection = golden.select([])
        summary = golden.summarize(selection, total=0)
        assert summary["golden"] == 0
        assert "golden set:            0" in golden.format_report(summary)


class TestIndexAndOutput:
    @pytest.fixture
    def index_path(self, tmp_path):
        data = index.IndexData(
            sources=[index.SourceRow("s1", "git", "https://x/r.git", "a" * 40, "Apache-2.0")],
            analyzer_version="9.9.9",
        )
        data.chunks = [
            index.ChunkRow(
                "s1", "a.md", "A", ("A",), "explanation", "client", "", "text",
                examples=[
                    index.ExampleRow("qna", "good query", ("1", "2"), (), "plural integer",
                                     False, "c", parses=True, unknown_inspectors=("idp user",)),
                    index.ExampleRow("qna", "broken query", ("1",), (), "", False, "c",
                                     parses=False, parse_error="bad"),
                    index.ExampleRow("qna", "error query", (), ("boom",), "", False, "c",
                                     parses=True),
                    index.ExampleRow("fence-relevance", "bare query", (), (), "", False, "c",
                                     parses=True),
                    index.ExampleRow("qna", "labelled query", ("x",), (), "", False, "c",
                                     parses=True, dialect="session", dialect_source="analyzer"),
                ],
            )
        ]  # fmt: skip
        path = tmp_path / "relevance.sqlite"
        index.write_index(path, data)
        return path

    def test_loading_reads_examples_meta_and_sources(self, index_path):
        candidates, meta, sources = golden.load_candidates(index_path)
        assert [c.query for c in candidates] == [
            "good query", "broken query", "error query", "bare query", "labelled query"
        ]  # fmt: skip
        assert candidates[0].answers == ("1", "2")
        assert candidates[0].unknown_inspectors == ("idp user",)
        assert candidates[1].parses is False
        assert meta["analyzer_version"] == "9.9.9"
        assert sources == [
            {"id": "s1", "kind": "git", "origin": "https://x/r.git", "revision": "a" * 40}
        ]

    def test_main_writes_the_jsonl_and_a_meta_sidecar(self, index_path, tmp_path, capsys):
        out = tmp_path / "eval" / "golden.jsonl"
        assert golden.main(["--index", str(index_path), "--out", str(out)]) == 0
        lines = out.read_text(encoding="ascii").splitlines()
        records = [json.loads(line) for line in lines]
        assert [r["query"] for r in records] == ["good query", "labelled query"]
        assert records[0]["answers"] == ["1", "2"]
        assert records[0]["result_type"] == "plural integer"
        assert records[0]["unknown_inspectors"] == ["idp user"]
        assert records[1]["dialect"] == "session"
        assert records[1]["dialect_source"] == "analyzer"
        meta = json.loads((out.parent / golden.META_NAME).read_text(encoding="ascii"))
        assert meta["criteria_version"] == golden.CRITERIA_VERSION
        assert meta["analyzer_version"] == "9.9.9"
        assert meta["index_schema_version"] == str(index.SCHEMA_VERSION)
        assert meta["sources"][0]["revision"] == "a" * 40
        assert meta["summary"]["golden"] == 2
        printed = capsys.readouterr().out
        assert "golden set:            2" in printed
        assert "does-not-parse" in printed

    def test_the_output_is_reproducible_byte_for_byte(self, index_path, tmp_path):
        digests = []
        for name in ("one", "two"):
            out = tmp_path / name / "golden.jsonl"
            golden.main(["--index", str(index_path), "--out", str(out)])
            digests.append(hashlib.sha256(out.read_bytes()).hexdigest())
        assert digests[0] == digests[1]

    def test_the_output_is_ascii_even_when_a_query_is_not(self, tmp_path):
        data = index.IndexData(sources=[index.SourceRow("s1", "git", "x", "y")])
        odd = "name of it " + chr(0x2019) + "s"
        data.chunks = [
            index.ChunkRow(
                "s1", "a.md", "A", ("A",), "explanation", "client", "", "t",
                examples=[index.ExampleRow("qna", odd, ("a",), (), "", False, "c", parses=True)],
            )
        ]  # fmt: skip
        path = tmp_path / "relevance.sqlite"
        index.write_index(path, data)
        out = tmp_path / "golden.jsonl"
        assert golden.main(["--index", str(path), "--out", str(out)]) == 0
        assert json.loads(out.read_text(encoding="ascii"))["query"] == odd

    def test_the_index_is_only_read(self, index_path, tmp_path):
        before = hashlib.sha256(index_path.read_bytes()).hexdigest()
        files = sorted(p.name for p in index_path.parent.iterdir())
        out_dir = tmp_path / "out"  # beside the index, so ignore it in the listing below
        golden.main(["--index", str(index_path), "--out", str(out_dir / "g.jsonl")])
        assert hashlib.sha256(index_path.read_bytes()).hexdigest() == before
        after = sorted(p.name for p in index_path.parent.iterdir() if p.name != out_dir.name)
        assert after == files  # no journal or temp file appeared next to the index

    def test_a_missing_index_is_a_clean_exit_2_and_writes_nothing(self, tmp_path):
        out = tmp_path / "eval" / "golden.jsonl"
        assert golden.main(["--index", str(tmp_path / "nope.sqlite"), "--out", str(out)]) == 2
        assert not out.exists()

    def test_an_index_from_the_previous_schema_is_a_clean_exit_2(self, tmp_path):
        old = tmp_path / "old.sqlite"
        connection = sqlite3.connect(old)
        connection.execute(
            "CREATE TABLE examples (id INTEGER PRIMARY KEY, source_id, path, dialect, form,"
            " query, answers, errors, result_type, incomplete)"
        )
        connection.commit()
        connection.close()
        out = tmp_path / "golden.jsonl"
        assert golden.main(["--index", str(old), "--out", str(out)]) == 2
        assert not out.exists()

"""tools/eval/retrieval.py: sanity measurements of search over the index."""

import hashlib
import os

import pytest

from bigfix_root_mcp import knowledge
from tools.eval import retrieval as rv
from tools.ingest import index


def chunk_key(heading="H", path="p.md", kind="explanation", text="t"):
    return rv.ChunkKey(path=path, heading=heading, kind=kind, text=text)


def example_key(query="q", path="p.md", form="qna"):
    return rv.ExampleKey(query=query, path=path, form=form)


class Scripted:
    """Stands in for knowledge.search: returns a scripted result list per query text."""

    def __init__(self, examples=None, chunks=None) -> None:
        self.examples = examples or {}
        self.chunks = chunks or {}
        self.calls = []

    def __call__(self, text, dialect=None, kind=None, limit=5):
        self.calls.append((text, kind, limit))
        return {
            "examples": [{"query": q} for q in self.examples.get(text, [])],
            "chunks": [
                {"path": p, "heading": h, "text": t} for p, h, t in self.chunks.get(text, [])
            ],
        }


class TestSelfRetrieval:
    def test_ranks_are_counted_from_one(self, monkeypatch):
        script = Scripted(
            examples={"first one": ["first one", "other"], "second one": ["x", "second one"]}
        )
        monkeypatch.setattr(rv.knowledge, "search", script)
        found = rv.self_retrieval([example_key("first one"), example_key("second one")])
        assert (found["total"], found["at_1"], found["at_k"], found["not_found"]) == (2, 1, 2, 0)
        assert found["rate_at_1"] == 0.5
        assert found["rate_at_k"] == 1.0

    def test_a_missing_example_is_reported_with_its_path(self, monkeypatch):
        monkeypatch.setattr(rv.knowledge, "search", Scripted(examples={"lost one": ["other"]}))
        found = rv.self_retrieval([example_key("lost one", path="a/b.md")])
        assert found["not_found"] == 1
        assert found["misses"] == [("a/b.md", "lost one")]

    def test_a_query_with_nothing_searchable_is_skipped_not_counted(self, monkeypatch):
        script = Scripted()
        monkeypatch.setattr(rv.knowledge, "search", script)
        found = rv.self_retrieval([example_key("= + ;;")])
        assert (found["total"], found["skipped"]) == (0, 1)
        assert script.calls == []

    def test_only_examples_are_searched_and_the_window_is_the_top_k(self, monkeypatch):
        script = Scripted(examples={"q one": ["q one"]})
        monkeypatch.setattr(rv.knowledge, "search", script)
        rv.self_retrieval([example_key("q one")])
        assert script.calls == [("q one", "example", rv.TOP_K)]

    def test_a_long_query_is_cut_to_what_the_search_accepts(self, monkeypatch):
        script = Scripted()
        monkeypatch.setattr(rv.knowledge, "search", script)
        rv.self_retrieval([example_key("word " * 200)])
        assert len(script.calls[0][0]) == knowledge.MAX_QUERY_CHARS

    def test_an_empty_run_has_zero_rates_not_a_division_error(self):
        found = rv.self_retrieval([])
        assert (found["total"], found["rate_at_1"], found["rate_at_k"]) == (0, 0.0, 0.0)


class TestHeadingLookup:
    def test_rank_one_is_a_hit_and_anything_else_is_a_failure_with_its_cause(self, monkeypatch):
        script = Scripted(
            chunks={
                "Alpha": [("a.md", "Alpha", "x")],
                "Beta": [("z.md", "Other", "y"), ("b.md", "Beta", "z")],
                "Gamma": [("z.md", "Other", "y")],
            }
        )
        monkeypatch.setattr(rv.knowledge, "search", script)
        found = rv.heading_lookup(
            [
                chunk_key("Alpha", "a.md"),
                chunk_key("Beta", "b.md"),
                chunk_key("Gamma", "g.md"),
            ]
        )
        assert (found["total"], found["at_1"], found["at_k"], found["not_found"]) == (3, 1, 2, 1)
        causes = {heading: cause for _, heading, cause in found["failures"]}
        assert causes == {"Beta": "ranked 2", "Gamma": "not in the top results"}

    def test_a_section_split_into_several_chunks_is_asked_about_once(self, monkeypatch):
        script = Scripted(chunks={"Long": [("l.md", "Long", "part one")]})
        monkeypatch.setattr(rv.knowledge, "search", script)
        found = rv.heading_lookup(
            [chunk_key("Long", "l.md", text="one"), chunk_key("Long", "l.md", text="two")]
        )
        assert found["total"] == 1
        assert len(script.calls) == 1

    def test_the_search_is_restricted_to_the_chunks_own_kind(self, monkeypatch):
        script = Scripted(chunks={"Sig": [("a.md", "Sig", "t")]})
        monkeypatch.setattr(rv.knowledge, "search", script)
        rv.heading_lookup([chunk_key("Sig", "a.md", kind="inspector")])
        assert script.calls[0][1] == "inspector"

    def test_result_lists_holding_a_duplicate_text_are_counted(self, monkeypatch):
        script = Scripted(
            chunks={
                "Dup": [("d1.md", "Dup", "same"), ("d2.md", "Dup", "same")],
                "Uniq": [("u.md", "Uniq", "one"), ("v.md", "Uniq", "two")],
            }
        )
        monkeypatch.setattr(rv.knowledge, "search", script)
        found = rv.heading_lookup([chunk_key("Dup", "d1.md"), chunk_key("Uniq", "u.md")])
        assert found["crowded_result_lists"] == 1

    def test_a_heading_with_nothing_searchable_is_skipped(self, monkeypatch):
        script = Scripted()
        monkeypatch.setattr(rv.knowledge, "search", script)
        assert rv.heading_lookup([chunk_key("<> : ;")])["total"] == 0
        assert script.calls == []


class TestDuplicateChunks:
    def test_texts_on_several_pages_are_found_and_grouped(self):
        found = rv.duplicate_chunks(
            [
                chunk_key(path="a.md", text="shared 1"),
                chunk_key(path="b.md", text="shared 1"),
                chunk_key(path="a.md", text="shared 2"),
                chunk_key(path="b.md", text="shared 2"),
                chunk_key(path="a.md", text="only a"),
                chunk_key(path="c.md", text="three way"),
                chunk_key(path="d.md", text="three way"),
                chunk_key(path="e.md", text="three way"),
            ]
        )
        assert found["distinct_texts"] == 4
        assert found["texts_on_several_pages"] == 3
        assert found["redundant_chunks"] == 1 + 1 + 2  # a 3-page text has 2 redundant copies
        assert found["page_groups"][0] == (("a.md", "b.md"), 2)

    def test_the_same_text_twice_on_one_page_is_not_a_duplicate_page(self):
        found = rv.duplicate_chunks(
            [chunk_key(path="a.md", text="x"), chunk_key(path="a.md", text="x")]
        )
        assert found["texts_on_several_pages"] == 0
        assert found["redundant_chunks"] == 0


class TestEndToEnd:
    @pytest.fixture
    def index_path(self, tmp_path):
        data = index.IndexData(sources=[index.SourceRow("s1", "git", "https://x/r.git", "a" * 40)])

        def chunk(heading, text, path, examples=(), kind="explanation"):
            return index.ChunkRow(
                "s1", path, heading, ("doc", heading), kind, "client", "", text, list(examples)
            )

        def example(query):
            return index.ExampleRow("qna", query, ("a",), (), "", False, f"doc > {query}")

        data.chunks = [
            chunk("Unique topic", "Text about quarks only.", "u.md",
                  [example("names of quark things")]),
            chunk("Registry keys", "Reading registry values.", "r.md",
                  [example('value "Version" of key "HKLM" of registry')]),
            chunk("Twin page", "Identical body text here.", "t1.md"),
            chunk("Twin page", "Identical body text here.", "t2.md"),
        ]  # fmt: skip
        path = tmp_path / "relevance.sqlite"
        index.write_index(path, data)
        return path

    def test_measure_runs_every_check_against_a_real_index(self, index_path):
        results = rv.measure(index_path)
        assert results["self_retrieval"]["total"] == 2
        assert results["self_retrieval"]["rate_at_1"] == 1.0
        assert results["heading_lookup"]["total"] == 4
        # the second twin is ranked after the first, and both lists hold identical texts
        assert results["heading_lookup"]["at_1"] == 3
        assert results["heading_lookup"]["crowded_result_lists"] == 2
        assert results["duplicates"]["page_groups"] == [(("t1.md", "t2.md"), 1)]

    def test_the_index_path_environment_variable_is_restored(self, index_path, monkeypatch):
        monkeypatch.setenv(knowledge.INDEX_ENV, "/somewhere/else.sqlite")
        rv.measure(index_path)
        assert os.environ[knowledge.INDEX_ENV] == "/somewhere/else.sqlite"
        monkeypatch.delenv(knowledge.INDEX_ENV)
        rv.measure(index_path)
        assert knowledge.INDEX_ENV not in os.environ

    def test_the_index_is_only_read(self, index_path):
        before = hashlib.sha256(index_path.read_bytes()).hexdigest()
        files = sorted(p.name for p in index_path.parent.iterdir())
        rv.measure(index_path)
        assert hashlib.sha256(index_path.read_bytes()).hexdigest() == before
        assert sorted(p.name for p in index_path.parent.iterdir()) == files

    def test_main_prints_a_report_that_says_what_the_scores_do_not_show(self, index_path, capsys):
        assert rv.main(["--index", str(index_path)]) == 0
        output = capsys.readouterr().out
        assert "mechanics checks" in output
        assert "not evidence that real questions are answered" in output
        assert "self-retrieval: search an example by its own text (2 examples" in output
        assert "heading lookup: search a chunk by its heading (4 distinct headings)" in output
        assert "t1.md = t2.md" in output

    def test_a_missing_index_is_a_clean_exit_2(self, tmp_path, capsys):
        assert rv.main(["--index", str(tmp_path / "nope.sqlite")]) == 2
        assert "self-retrieval" not in capsys.readouterr().out


class TestReport:
    def test_an_empty_run_formats_without_dividing_by_zero(self):
        empty = {
            "self_retrieval": rv.self_retrieval([]),
            "heading_lookup": {
                **rv._summary([]),
                "total": 0,
                "crowded_result_lists": 0,
                "failures": [],
            },
            "duplicates": rv.duplicate_chunks([]),
        }
        text = rv.format_report(empty)
        assert "(0 examples" in text
        assert "n/a" in text

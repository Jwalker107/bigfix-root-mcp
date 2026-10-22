"""knowledge.py and the search_relevance_docs tool: search over a local index."""

import ast
import hashlib
import sqlite3

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from bigfix_root_mcp import knowledge, server
from tools.ingest import index

ENV = knowledge.INDEX_ENV
SHA = "a" * 40


def chunk(
    heading, text, *, kind="explanation", dialect="both", source="s1", examples=(), path=None
):
    return index.ChunkRow(
        source,
        path or f"{heading.lower().replace(' ', '-')}.md",
        heading,
        ("doc", heading),
        kind,
        dialect,
        "8.1" if source == "s2" else "",
        text,
        list(examples),
    )


def example(
    query, answers=(), errors=(), *, result_type="", incomplete=False, form="qna", **verdict
):
    """An example row; `verdict` carries the analyzer fields (parses, dialect_source, ...)."""
    return index.ExampleRow(
        form,
        query,
        tuple(answers),
        tuple(errors),
        result_type,
        incomplete,
        f"doc > {query}",
        **verdict,
    )


def sample_data():
    data = index.IndexData(
        sources=[
            index.SourceRow(
                "s1", "git", "https://example.com/r.git", SHA, "Apache-2.0", "", "both"
            ),
            index.SourceRow(
                "s2", "url", "https://example.com/g.pdf", "b" * 64, "Public", "8.1", "client"
            ),
        ]
    )
    data.chunks = [
        chunk(
            "left shift <integer> of <bit set> : bit set",
            "Shift the bit set left n times.",
            kind="inspector",
            dialect="unknown",
            examples=[
                example(
                    'left shift 2 of bit set "10101"', ["1010100"], result_type="singular bit set"
                )
            ],
        ),
        chunk(
            "Files",
            "The files of folder inspector lists files in a folder.",
            dialect="client",
            examples=[example('names of files of folder "c:"', ["a.txt", "b.txt"])],
        ),
        chunk(
            "Computers",
            "The bes computers inspector lists the computers visible to the console.",
            dialect="session",
            examples=[example("names of bes computers", form="fence-relevance")],
        ),
        chunk("Strings", "A string is text. Use name of it to show a name."),
        chunk("Decoy", "files files files files files"),
        chunk("Long Reference", "lorem " * 600),
        chunk(
            "Users",
            "Properties of a user.",
            examples=[
                example(
                    'properties of type "user"', [f"answer {n}" for n in range(30)], incomplete=True
                ),
                example("sha1 of file", ["x" * 500]),
                example("bad query", errors=["A singular expression is required."]),
            ],
        ),
        chunk(
            "Verdicts",
            "Samples of analyzer verdicts.",
            examples=[
                example("verdict clean query", ["ok"], parses=True),
                example(
                    "verdict broken query",
                    ["no"],
                    parses=False,
                    parse_error="line 1, column 9: expected ')'",
                ),
                example(
                    "verdict documented error query",
                    errors=["boom"],
                    parses=False,
                    parse_error="line 1: unexpected token",
                ),
                example(
                    "verdict unknown inspector query",
                    ["x"],
                    parses=True,
                    unknown_inspectors=("idp user", "ips image"),
                ),
                example(
                    "verdict inferred dialect query",
                    ["y"],
                    parses=True,
                    dialect="session",
                    dialect_source="analyzer",
                ),
                example("verdict not analysed query", ["z"]),
            ],
        ),
        chunk("Registry", "Registry keys.", source="s2", dialect="client"),
        *[chunk(f"Filler {n}", "common marker word") for n in range(30)],
    ]
    return data


@pytest.fixture
def index_path(tmp_path, monkeypatch):
    path = tmp_path / "relevance.sqlite"
    index.write_index(path, sample_data())
    monkeypatch.setenv(ENV, str(path))
    return path


def headings(result, key="chunks"):
    return [item["heading"] for item in result[key]]


class TestParseQuery:
    def test_words_and_quoted_phrases(self):
        assert knowledge.parse_query('bes "files of folder" computers') == [
            ["files", "of", "folder"],
            ["bes"],
            ["computers"],
        ]

    def test_punctuation_never_survives(self):
        terms = knowledge.parse_query("<bit set> : boolean; a_b (x)")
        assert terms == [["bit"], ["set"], ["boolean"], ["a"], ["b"], ["x"]]

    def test_duplicates_are_dropped_ignoring_case(self):
        assert knowledge.parse_query("Files files FILES") == [["Files"]]

    def test_an_unbalanced_quote_is_harmless(self):
        assert knowledge.parse_query('"files of') == [["files"], ["of"]]

    def test_non_ascii_letters_are_kept(self):
        word = chr(0x3B1) * 3  # Greek letters: letters in Unicode, not ASCII
        assert knowledge.parse_query(word) == [[word]]

    def test_nothing_searchable(self):
        assert knowledge.parse_query('<> ""  ;;; _') == []


class TestSearch:
    def test_finds_an_inspector_by_its_signature_words(self, index_path):
        result = knowledge.search("left shift bit set")
        assert result["available"] is True
        assert headings(result)[0] == "left shift <integer> of <bit set> : bit set"
        first = result["chunks"][0]
        assert (first["kind"], first["dialect"], first["source"]) == ("inspector", "unknown", "s1")
        assert first["section_path"] == "doc > left shift <integer> of <bit set> : bit set"
        assert result["match"] == {"chunks": "all_terms", "examples": "all_terms"}

    def test_stemming_matches_plurals(self, index_path):
        # the text says "computers"; the query says "computer"
        assert "Computers" in headings(knowledge.search("computer"))

    def test_a_heading_hit_outranks_repeated_body_words(self, index_path):
        # "Decoy" repeats the word five times in its body; "Files" has it in the heading
        assert headings(knowledge.search("files", kind="explanation"))[:2] == ["Files", "Decoy"]

    def test_all_terms_then_any_terms(self, index_path):
        strict = knowledge.search("shift bit")
        assert strict["match"]["chunks"] == "all_terms"
        loose = knowledge.search("shift unicorn")
        assert loose["match"]["chunks"] == "any_terms"
        assert headings(loose)[0].startswith("left shift")

    def test_nothing_found_is_reported_as_none(self, index_path):
        result = knowledge.search("unicorn")
        assert result["chunks"] == []
        assert result["examples"] == []
        assert result["match"] == {"chunks": "none", "examples": "none"}
        assert result["index"]["sources"] == []

    def test_a_quoted_phrase_must_match_in_order(self, index_path):
        assert headings(knowledge.search('"bit set left"'))[:1] == [
            "left shift <integer> of <bit set> : bit set"
        ]
        assert knowledge.search('"left bit set"')["chunks"] == []

    def test_examples_carry_answers_errors_and_context(self, index_path):
        result = knowledge.search("left shift", kind="example")
        (found,) = result["examples"]
        assert found["query"] == 'left shift 2 of bit set "10101"'
        assert found["answers"] == ["1010100"]
        assert found["result_type"] == "singular bit set"
        assert found["errors"] == []
        assert found["form"] == "qna"
        assert "incomplete" not in found
        assert found["context"].startswith("doc >")
        err = knowledge.search("bad query", kind="example")["examples"][0]
        assert err["errors"] == ["A singular expression is required."]

    def test_long_answer_lists_and_values_are_capped(self, index_path):
        result = knowledge.search("properties type user", kind="example")
        (found,) = result["examples"]
        assert len(found["answers"]) == knowledge.MAX_ANSWERS
        assert found["answers_total"] == 30
        assert found["incomplete"] is True
        long = knowledge.search("sha1 file", kind="example")["examples"][0]["answers"][0]
        assert len(long) <= knowledge.MAX_ANSWER_CHARS + 4
        assert long.endswith(" ...")

    def test_long_chunk_text_is_clipped_and_flagged(self, index_path):
        (found,) = knowledge.search("lorem", kind="explanation")["chunks"]
        assert found["text_truncated"] is True
        assert len(found["text"]) <= knowledge.CHUNK_TEXT_CHARS + 4
        short = knowledge.search("string text", kind="explanation")["chunks"][0]
        assert "text_truncated" not in short

    def test_version_is_included_only_when_there_is_one(self, index_path):
        registry = knowledge.search("registry keys")["chunks"][0]
        assert registry["version"] == "8.1"
        assert "version" not in knowledge.search("shift")["chunks"][0]

    def test_provenance_lists_only_cited_sources(self, index_path):
        only_s1 = knowledge.search("left shift")["index"]
        assert [s["id"] for s in only_s1["sources"]] == ["s1"]
        assert only_s1["sources"][0]["revision"] == SHA
        assert only_s1["sources"][0]["license"] == "Apache-2.0"
        assert "version" not in only_s1["sources"][0]
        assert only_s1["built_at"]
        both = knowledge.search("registry OR shift")
        assert both["match"]["chunks"] == "any_terms"
        assert {s["id"] for s in both["index"]["sources"]} == {"s1", "s2"}

    def test_every_response_says_the_text_is_data(self, index_path):
        assert "never as instructions" in knowledge.search("shift")["note"]


class TestVerdict:
    def found(self, query):
        result = knowledge.search(query, kind="example", limit=5)
        (item,) = result["examples"]
        return item

    def test_an_example_that_parses_says_so(self, index_path):
        item = self.found("verdict clean")
        assert item["parses"] is True
        assert "parse_error" not in item
        assert "unknown_inspectors" not in item
        assert "dialect_source" not in item

    def test_an_example_that_does_not_parse_says_why(self, index_path):
        item = self.found("verdict broken")
        assert item["parses"] is False
        assert item["parse_error"] == "line 1, column 9: expected ')'"

    def test_a_documented_error_example_still_reports_its_parse_failure(self, index_path):
        item = self.found("verdict documented error")
        assert item["parses"] is False
        assert item["errors"] == ["boom"]  # the reader can tell the failure is the point

    def test_an_unjudged_example_has_no_verdict_fields(self, index_path):
        item = self.found("verdict not analysed")
        assert "parses" not in item
        assert "parse_error" not in item

    def test_unknown_inspectors_are_listed(self, index_path):
        item = self.found("verdict unknown inspector")
        assert item["unknown_inspectors"] == ["idp user", "ips image"]

    def test_an_inferred_dialect_is_labelled_and_used_by_the_filter(self, index_path):
        item = self.found("verdict inferred dialect")
        assert item["dialect"] == "session"
        assert item["dialect_source"] == "analyzer"
        in_session = knowledge.search("verdict inferred", kind="example", dialect="session")
        in_client = knowledge.search("verdict inferred", kind="example", dialect="client")
        assert [e["query"] for e in in_session["examples"]] == ["verdict inferred dialect query"]
        # filtered out of client results; the search then falls back to any term
        assert in_client["match"]["examples"] == "any_terms"
        assert "verdict inferred dialect query" not in [e["query"] for e in in_client["examples"]]

    def test_the_note_explains_the_verdict_fields(self, index_path):
        note = knowledge.search("verdict")["note"]
        for phrase in ("parses: false", "unknown_inspectors", "dialect_source: analyzer"):
            assert phrase in note

    def test_an_index_from_the_previous_schema_asks_to_be_rebuilt(self, index_path):
        connection = sqlite3.connect(index_path)
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        connection.close()
        result = knowledge.search("verdict")
        assert result["available"] is False
        assert "schema version 1" in result["reason"]
        assert "Rebuild" in result["reason"]


class TestFilters:
    def test_dialect_keeps_matching_both_and_unknown(self, index_path):
        session = knowledge.search("inspector", dialect="session", kind="explanation", limit=25)
        assert "Computers" in headings(session)
        assert "Files" not in headings(session)
        client = knowledge.search("inspector", dialect="client", kind="explanation", limit=25)
        assert "Files" in headings(client)
        assert "Computers" not in headings(client)

    def test_unknown_dialect_matches_either(self, index_path):
        for dialect in ("session", "client"):
            result = knowledge.search("left shift", dialect=dialect)
            assert headings(result)[0].startswith("left shift")
            assert result["examples"][0]["dialect"] == "unknown"

    def test_dialect_applies_to_examples_too(self, index_path):
        session = knowledge.search("names", kind="example", dialect="session")
        assert [e["query"] for e in session["examples"]] == ["names of bes computers"]

    def test_kind_narrows_the_lists_returned(self, index_path):
        assert set(knowledge.search("shift", kind="inspector")) >= {"chunks"}
        assert "examples" not in knowledge.search("shift", kind="inspector")
        assert headings(knowledge.search("shift", kind="inspector")) == [
            "left shift <integer> of <bit set> : bit set"
        ]
        assert "chunks" not in knowledge.search("shift", kind="example")
        both = knowledge.search("shift")
        assert "chunks" in both
        assert "examples" in both

    def test_inputs_are_normalised(self, index_path):
        assert knowledge.search("shift", dialect=" Session ", kind=" INSPECTOR ")["available"]

    @pytest.mark.parametrize(
        ("arguments", "message"),
        [
            ({"query": ""}, "must not be empty"),
            ({"query": "   "}, "must not be empty"),
            ({"query": "<> ;;"}, "no searchable terms"),
            ({"query": "x" * (knowledge.MAX_QUERY_CHARS + 1)}, "too long"),
            ({"query": "a", "dialect": "bogus"}, "dialect must be one of"),
            ({"query": "a", "kind": "bogus"}, "kind must be one of"),
        ],
    )
    def test_bad_arguments_are_value_errors(self, index_path, arguments, message):
        with pytest.raises(ValueError, match=message):
            knowledge.search(**arguments)

    def test_arguments_are_checked_before_the_index_is_opened(self, monkeypatch):
        monkeypatch.delenv(ENV, raising=False)
        with pytest.raises(ValueError, match="dialect"):
            knowledge.search("a", dialect="bogus")


class TestLimits:
    def test_limit_is_clamped_in_both_directions(self, index_path):
        assert len(knowledge.search("marker", kind="explanation", limit=1000)["chunks"]) == (
            knowledge.MAX_SEARCH_LIMIT
        )
        assert len(knowledge.search("marker", kind="explanation", limit=0)["chunks"]) == 1
        assert len(knowledge.search("marker", kind="explanation", limit=-5)["chunks"]) == 1

    def test_the_default_limit_applies_per_list(self, index_path):
        assert len(knowledge.search("marker")["chunks"]) == knowledge.DEFAULT_LIMIT

    def test_a_long_query_keeps_only_the_first_terms(self, index_path):
        words = [f"word{n}" for n in range(knowledge.MAX_TERMS + 3)]
        result = knowledge.search(" ".join(words) + " shift")
        assert len(result["terms"]) == knowledge.MAX_TERMS
        assert result["terms_ignored"] == 4


class TestHostileQueries:
    @pytest.mark.parametrize(
        "query",
        [
            '"',
            '""',
            '" OR 1=1 --',
            "NEAR(a b)",
            "a*",
            "-a",
            "col:val",
            "heading:shift",
            "a AND",
            "(",
            ")",
            "^a",
            "{x}",
            "'; DROP TABLE chunks; --",
            "shift\x00bit",
        ],
    )
    def test_fts_syntax_is_just_words(self, index_path, query):
        try:
            result = knowledge.search(query)
        except ValueError as err:
            assert "searchable" in str(err)  # nothing left after punctuation is removed
        else:
            assert result["available"] is True
        connection = sqlite3.connect(index_path)
        assert connection.execute("SELECT count(*) FROM chunks").fetchone()[0] > 0
        connection.close()

    def test_a_column_filter_in_a_query_is_not_interpreted(self, index_path):
        # if "heading:" were an FTS column filter this would find the heading only
        result = knowledge.search("heading:unicorn")
        assert result["terms"] == ["heading", "unicorn"]
        assert result["chunks"] == []


class TestAvailability:
    def test_unset(self, monkeypatch):
        monkeypatch.delenv(ENV, raising=False)
        result = knowledge.search("shift")
        assert result["available"] is False
        assert ENV in result["reason"]
        assert "tools.ingest.build" in result["how_to_enable"]
        assert "chunks" not in result

    def test_blank_counts_as_unset(self, monkeypatch):
        monkeypatch.setenv(ENV, "   ")
        assert ENV in knowledge.search("shift")["reason"]

    def test_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, str(tmp_path / "nope.sqlite"))
        result = knowledge.search("shift")
        assert result["available"] is False
        assert "no index file" in result["reason"]

    def test_a_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, str(tmp_path))
        assert "no index file" in knowledge.search("shift")["reason"]

    def test_a_file_that_is_not_a_database(self, tmp_path, monkeypatch):
        path = tmp_path / "junk.sqlite"
        path.write_bytes(b"this is not a database " * 100)
        monkeypatch.setenv(ENV, str(path))
        result = knowledge.search("shift")
        assert result["available"] is False
        assert "cannot be read" in result["reason"]

    def test_a_database_that_is_not_an_index(self, tmp_path, monkeypatch):
        path = tmp_path / "other.sqlite"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE unrelated (x)")
        connection.commit()
        connection.close()
        monkeypatch.setenv(ENV, str(path))
        assert "not a knowledge index" in knowledge.search("shift")["reason"]

    def test_a_schema_version_this_server_does_not_read(self, index_path):
        connection = sqlite3.connect(index_path)
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
        connection.close()
        reason = knowledge.search("shift")["reason"]
        assert "schema version 99" in reason
        assert f"version {knowledge.SUPPORTED_SCHEMA_VERSION}" in reason

    def test_the_path_is_read_on_every_call(self, index_path, monkeypatch, tmp_path):
        assert knowledge.search("shift")["available"] is True
        monkeypatch.setenv(ENV, str(tmp_path / "gone.sqlite"))
        assert knowledge.search("shift")["available"] is False

    def test_a_user_home_shortcut_is_expanded(self, index_path, monkeypatch):
        home = index_path.parent
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv(ENV, "~/" + index_path.name)
        assert knowledge.search("shift")["available"] is True


class TestReadOnly:
    def test_searching_changes_nothing_on_disk(self, index_path):
        before_bytes = hashlib.sha256(index_path.read_bytes()).hexdigest()
        before_files = sorted(p.name for p in index_path.parent.iterdir())
        for query in ("shift", "files", "unicorn", "registry OR shift"):
            knowledge.search(query)
        assert hashlib.sha256(index_path.read_bytes()).hexdigest() == before_bytes
        assert sorted(p.name for p in index_path.parent.iterdir()) == before_files

    def test_a_read_only_file_can_be_searched(self, index_path):
        index_path.chmod(0o444)
        try:
            assert knowledge.search("shift")["available"] is True
        finally:
            index_path.chmod(0o644)

    def test_the_connection_cannot_write(self, index_path):
        connection, problem = knowledge._open_index()
        assert problem is None
        try:
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("DELETE FROM chunks")
        finally:
            connection.close()


class TestSchemaContract:
    def test_the_reader_and_the_writer_agree_on_the_schema_version(self):
        assert knowledge.SUPPORTED_SCHEMA_VERSION == index.SCHEMA_VERSION

    def test_the_tables_the_reader_needs_are_all_written(self, index_path):
        connection = sqlite3.connect(index_path)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        connection.close()
        assert knowledge._REQUIRED_TABLES <= tables


class TestTool:
    async def call(self, arguments):
        async with Client(server.mcp) as client:
            return await client.call_tool("search_relevance_docs", arguments)

    async def test_the_tool_returns_results(self, index_path):
        result = await self.call({"query": "left shift bit set", "kind": "inspector"})
        assert result.data["available"] is True
        assert result.data["chunks"][0]["heading"].startswith("left shift")
        assert "examples" not in result.data

    async def test_the_tool_passes_every_argument_through(self, index_path):
        result = await self.call(
            {"query": "marker", "dialect": "client", "kind": "explanation", "limit": 2}
        )
        assert len(result.data["chunks"]) == 2

    async def test_an_unconfigured_index_is_an_answer_not_an_error(self, monkeypatch):
        monkeypatch.delenv(ENV, raising=False)
        result = await self.call({"query": "shift"})
        assert result.data["available"] is False
        assert result.data["how_to_enable"]

    async def test_bad_arguments_become_tool_errors_that_say_why(self, index_path):
        with pytest.raises(ToolError, match=r"search_relevance_docs: dialect must be one of"):
            await self.call({"query": "a", "dialect": "bogus"})
        with pytest.raises(ToolError, match="no searchable terms"):
            await self.call({"query": "<>"})

    async def test_the_tool_is_described_for_a_model(self):
        async with Client(server.mcp) as client:
            tool = next(t for t in await client.list_tools() if t.name == "search_relevance_docs")
        assert "analyze_relevance" in tool.description
        assert "not instructions" in tool.description
        assert set(tool.input_schema["properties"]) == {"query", "dialect", "kind", "limit"}
        assert tool.input_schema["required"] == ["query"]


def test_the_module_does_not_import_fastmcp():
    """Layering rule shared with analysis.py and clientquery.py."""
    with open(knowledge.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name.split(".")[0] == "fastmcp"}

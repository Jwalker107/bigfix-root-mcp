"""reference.py and the search_reference_fixlets tool: search over the fixlet index."""

import ast
import hashlib
import sqlite3

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from bigfix_root_mcp import reference, server
from tools.ingest import fixletbuild, fixletindex, fixlets

# Fake credentials are assembled from fragments so no real-looking token sits in this file.
AWS_KEY = "".join(("AK", "IA", "ABCDEFGHIJKLMNOP"))
GUARD = 'if exists property "in proxy agent context" then not in proxy agent context else true'


def document(*bodies):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' + "".join(bodies) + "</BES>"
    )


def fixlet_xml(title, description, *lines, script="run it"):
    rel = "".join(f"<Relevance>{line}</Relevance>" for line in lines)
    return (
        f"<Fixlet><Title>{title}</Title><Description>{description}</Description>{rel}"
        f"<Action><ActionScript>{script}</ActionScript></Action></Fixlet>"
    )


def analysis_xml(title, *props, applicability=("windows of operating system",)):
    rel = "".join(f"<Relevance>{line}</Relevance>" for line in applicability)
    body = "".join(f'<Property Name="{n}">{r}</Property>' for n, r in props)
    return f"<Analysis><Title>{title}</Title><Description>d</Description>{rel}{body}</Analysis>"


def write_tree(root, files):
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def index_path(tmp_path, monkeypatch):
    team = write_tree(
        tmp_path / "team",
        {
            "defender.bes": document(
                fixlet_xml(
                    "Add Defender exclusion",
                    "Excludes the client folder from Defender scanning",
                    "windows of operating system",
                    GUARD,
                    script="powershell Add-MpPreference",
                )
            ),
            "mac.bes": document(
                fixlet_xml(
                    "Mac scan", "Runs the scan", "mac of operating system", "version of it >= 1"
                )
            ),
            "nvidia.bes": document(
                analysis_xml("Nvidia driver", ("Driver", 'values "DriverVersion" of keys "x"'))
            ),
            "secret.bes": document(
                fixlet_xml("Leaky", "Sets a key", "true", script=f"set key {AWS_KEY}")
            ),
        },
    )
    public = write_tree(
        tmp_path / "public",
        {
            "pub.bes": document(
                fixlet_xml(
                    "Public defender sample",
                    "Another defender example",
                    "windows of operating system",
                )
            ),
        },
    )
    reads = []
    for name, tree, group in (("team-src", team, "team"), ("pub-src", public, "reference")):
        row = {**fixlets.adhoc_row(directory=str(tree), group=group), "id": name}
        read = fixlets.read_source(row, tmp_path / "raw")
        assert read.error == ""
        reads.append(read)
    built = fixletbuild.assemble(reads, min_files=1, min_share=0.0)
    path = tmp_path / "idx" / "fixlets.sqlite"
    fixletindex.write_index(path, built.data)
    monkeypatch.setenv(reference.INDEX_ENV, str(path))
    monkeypatch.delenv(reference.HOLDOUT_ENV, raising=False)
    return path


def titles(result):
    return [r["title"] for r in result["results"]]


class TestUnavailable:
    def test_unset_is_an_answer_that_says_how_to_enable(self, monkeypatch):
        monkeypatch.delenv(reference.INDEX_ENV, raising=False)
        result = reference.search("defender")
        assert result["available"] is False
        assert reference.INDEX_ENV in result["reason"]
        assert "fixletbuild" in result["how_to_enable"]

    def test_a_missing_file_is_named(self, monkeypatch, tmp_path):
        monkeypatch.setenv(reference.INDEX_ENV, str(tmp_path / "absent.sqlite"))
        assert "no index file" in reference.search("x")["reason"]

    def test_a_malformed_stored_field_is_an_answer_not_a_crash(self, index_path):
        connection = sqlite3.connect(index_path)
        connection.execute("UPDATE clauses SET relevance = 'not json'")
        connection.commit()
        connection.close()
        result = reference.search("defender exclusion")
        assert result["available"] is False
        assert "could not be searched" in result["reason"]

    def test_a_file_that_is_not_an_index_is_refused(self, monkeypatch, tmp_path):
        path = tmp_path / "other.sqlite"
        sqlite3.connect(path).execute("CREATE TABLE t (a)").connection.close()
        monkeypatch.setenv(reference.INDEX_ENV, str(path))
        assert "not a reference fixlet index" in reference.search("x")["reason"]

    def test_the_documentation_index_is_not_mistaken_for_it(self, monkeypatch, tmp_path):
        path = tmp_path / "docs.sqlite"
        with sqlite3.connect(path) as db:
            db.executescript("CREATE TABLE chunks (a); CREATE TABLE examples (a);")
        monkeypatch.setenv(reference.INDEX_ENV, str(path))
        assert reference.search("x")["available"] is False

    def test_a_schema_version_mismatch_says_to_rebuild(self, index_path):
        with sqlite3.connect(index_path) as db:
            db.execute("PRAGMA user_version = 99")
        assert "Rebuild" in reference.search("defender")["reason"]


class TestSearch:
    def test_the_purpose_text_finds_the_item(self, index_path):
        result = reference.search("defender exclusion")
        assert result["available"] is True
        assert titles(result)[0] == "Add Defender exclusion"

    def test_a_result_carries_what_a_model_needs(self, index_path):
        top = reference.search("defender exclusion", group="team", role="applicability")["results"][
            0
        ]
        assert top["role"] == "applicability"
        assert top["platform"] == "windows"
        assert top["group"] == "team"
        assert top["relevance"] == ["windows of operating system", GUARD]
        assert top["description"].startswith("Excludes the client folder")
        assert top["action_script"] == "powershell Add-MpPreference"
        assert top["parses"] is True
        assert len(top["id"]) == 12

    def test_the_relevance_itself_is_searchable(self, index_path):
        assert "Nvidia driver" in titles(reference.search("DriverVersion"))

    def test_a_property_names_itself(self, index_path):
        found = reference.search("nvidia", role="property")["results"]
        assert [r["property"] for r in found] == ["Driver"]

    def test_role_narrows(self, index_path):
        roles = {r["role"] for r in reference.search("nvidia", role="property")["results"]}
        assert roles == {"property"}
        assert reference.search("defender", role="property")["results"] == []

    def test_platform_keeps_that_platform_and_the_unclear(self, index_path):
        mac = reference.search("scan", platform="mac", role="applicability")["results"]
        assert [r["platform"] for r in mac] == ["mac"]
        assert reference.search("defender", platform="mac")["results"] == []

    def test_group_narrows_and_groups_are_listed(self, index_path):
        result = reference.search("defender")
        assert {g["group"] for g in result["groups"]} == {"team", "reference"}
        only = reference.search("defender", group="reference")["results"]
        assert {r["group"] for r in only} == {"reference"}

    def test_every_word_first_then_any(self, index_path):
        assert reference.search("defender exclusion")["match"] == "all_terms"
        assert reference.search("defender unicornword")["match"] == "any_terms"

    def test_limit_is_clamped(self, index_path):
        assert len(reference.search("defender OR scan OR nvidia", limit=1)["results"]) == 1
        assert reference.search("defender", limit=10_000)["available"] is True

    def test_query_syntax_is_just_words(self, index_path):
        for query in ('defender"', "NEAR(a b)", "defender*", "title:defender", "x OR y NOT z"):
            assert reference.search(query)["available"] is True

    def test_bad_arguments_say_why(self, index_path):
        with pytest.raises(ValueError, match="query must not be empty"):
            reference.search("  ")
        with pytest.raises(ValueError, match="too long"):
            reference.search("x" * 400)
        with pytest.raises(ValueError, match="no searchable terms"):
            reference.search("<>")
        with pytest.raises(ValueError, match="role must be"):
            reference.search("a", role="bogus")
        with pytest.raises(ValueError, match="platform must be"):
            reference.search("a", platform="beos")
        with pytest.raises(ValueError, match="group must be"):
            reference.search("a", group="has space")

    def test_role_and_platform_are_case_and_space_tolerant(self, index_path):
        assert reference.search("scan", role=" Applicability ", platform="MAC")["results"]


class TestDuplicates:
    def build(self, tmp_path, monkeypatch, groups):
        body = document(fixlet_xml("Same thing", "Does one job", "windows of operating system"))
        reads = []
        for n, group in enumerate(groups):
            tree = write_tree(tmp_path / f"t{n}", {"same.bes": body})
            row = {**fixlets.adhoc_row(directory=str(tree), group=group), "id": f"src{n}"}
            reads.append(fixlets.read_source(row, tmp_path / "raw"))
        built = fixletbuild.assemble(reads, min_files=1, min_share=0.0)
        path = tmp_path / "dup.sqlite"
        fixletindex.write_index(path, built.data)
        monkeypatch.setenv(reference.INDEX_ENV, str(path))

    def test_copies_in_one_group_collapse_to_one_result(self, tmp_path, monkeypatch):
        self.build(tmp_path, monkeypatch, ["team", "team", "team"])
        (only,) = reference.search("same thing", role="applicability")["results"]
        assert only["copies"] == 3

    def test_the_same_item_in_two_groups_is_reported_by_each(self, tmp_path, monkeypatch):
        self.build(tmp_path, monkeypatch, ["team", "reference"])
        results = reference.search("same thing", role="applicability")["results"]
        assert sorted(r["group"] for r in results) == ["reference", "team"]
        assert all("copies" not in r for r in results)

    def test_collapsing_still_fills_the_limit(self, tmp_path, monkeypatch):
        files = {
            f"f{n}.bes": document(fixlet_xml(f"Item {n}", "shared words", "true")) for n in range(3)
        }
        reads = []
        for n in range(3):  # three copies of each of three items
            tree = write_tree(tmp_path / f"c{n}", files)
            row = {**fixlets.adhoc_row(directory=str(tree), group="team"), "id": f"s{n}"}
            reads.append(fixlets.read_source(row, tmp_path / "raw"))
        built = fixletbuild.assemble(reads, min_files=1, min_share=0.0)
        path = tmp_path / "many.sqlite"
        fixletindex.write_index(path, built.data)
        monkeypatch.setenv(reference.INDEX_ENV, str(path))
        assert len(reference.search("shared words", limit=3)["results"]) == 3


class TestProvenance:
    def test_each_result_can_be_traced_to_a_read(self, index_path):
        result = reference.search("defender")
        sources = {s["source"]: s for s in result["index"]["sources"]}
        assert {"team-src", "pub-src"} <= set(sources)
        entry = sources["team-src"]
        assert entry["group"] == "team"
        assert entry["kind"] == "dir"
        assert entry["read_at"].endswith("Z")
        assert len(entry["fingerprint"]) == 16
        assert result["index"]["built_at"]

    def test_the_location_of_a_source_is_not_returned(self, index_path, tmp_path):
        text = repr(reference.search("defender"))
        assert str(tmp_path) not in text


class TestConventions:
    def test_counted_habits_come_with_the_results_for_that_group(self, index_path):
        result = reference.search("defender exclusion", group="team", role="applicability")
        guards = [c for c in result["conventions"] if c["pattern"] == GUARD]
        assert guards
        assert {c["group"] for c in result["conventions"]} == {"team"}
        # the team has four applicability sets: three fixlets and the analysis's own
        assert (guards[0]["items"], guards[0]["of"]) == (1, 4)

    def test_a_reference_group_is_reported_on_its_own(self, index_path):
        result = reference.search("defender", role="applicability")
        by_group = {c["group"] for c in result["conventions"]}
        assert by_group == {"team", "reference"}
        totals = {c["group"]: c["of"] for c in result["conventions"]}
        assert totals == {"team": 4, "reference": 1}  # each group's own count, never summed

    def test_properties_report_shapes(self, index_path):
        result = reference.search("nvidia", role="property")
        assert {c["kind"] for c in result["conventions"]} == {"shape"}

    def test_conventions_can_be_left_out(self, index_path):
        assert "conventions" not in reference.search("defender", include_conventions=False)

    def test_the_note_says_conventions_are_observations_not_rules(self, index_path):
        note = reference.search("defender")["note"]
        assert "outranks them" in note
        assert "never pooled" in note


class TestWhatIsWithheld:
    def test_a_field_the_scan_blocked_is_reported_and_its_text_absent(self, index_path):
        result = reference.search("leaky")
        (leaky,) = [r for r in result["results"] if r["title"] == "Leaky"]
        assert leaky["left_out"] == ["action"]
        assert "action_script" not in leaky
        assert AWS_KEY not in repr(result)

    def test_long_relevance_is_clipped_and_counted(self, tmp_path, monkeypatch):
        long_lines = [f"exists file {'x' * 800}{n}" for n in range(15)]
        tree = write_tree(
            tmp_path / "t", {"big.bes": document(fixlet_xml("Big one", "d", *long_lines))}
        )
        row = {**fixlets.adhoc_row(directory=str(tree)), "id": "big"}
        built = fixletbuild.assemble([fixlets.read_source(row, tmp_path / "raw")])
        path = tmp_path / "big.sqlite"
        fixletindex.write_index(path, built.data)
        monkeypatch.setenv(reference.INDEX_ENV, str(path))
        (top,) = reference.search("big one", role="applicability")["results"]
        assert len(top["relevance"]) == reference.RELEVANCE_LINES
        assert top["relevance_lines_total"] == 15
        assert all(len(line) <= reference.RELEVANCE_LINE_CHARS + 4 for line in top["relevance"])


class TestHoldOut:
    def test_held_out_items_are_never_returned(self, index_path, monkeypatch):
        top = reference.search("defender exclusion")["results"][0]
        monkeypatch.setenv(reference.HOLDOUT_ENV, top["id"])
        result = reference.search("defender exclusion")
        assert top["id"] not in [r["id"] for r in result["results"]]
        assert result["held_out"] == 1

    def test_a_malformed_list_makes_the_index_unavailable_not_ignored(
        self, index_path, monkeypatch
    ):
        monkeypatch.setenv(reference.HOLDOUT_ENV, "not-an-id")
        result = reference.search("defender")
        assert result["available"] is False
        assert "leak" in result["reason"]

    def test_blank_entries_are_ignored(self, index_path, monkeypatch):
        monkeypatch.setenv(reference.HOLDOUT_ENV, " , ")
        assert reference.search("defender")["available"] is True


class TestReadOnly:
    def test_searching_never_changes_the_file(self, index_path):
        before = hashlib.sha256(index_path.read_bytes()).hexdigest()
        files = sorted(p.name for p in index_path.parent.iterdir())
        for query in ("defender", "nvidia", "unicornword", "a OR b"):
            reference.search(query)
        assert hashlib.sha256(index_path.read_bytes()).hexdigest() == before
        assert sorted(p.name for p in index_path.parent.iterdir()) == files

    def test_the_connection_cannot_write(self, index_path):
        connection, problem = reference._open_index()
        assert problem is None
        try:
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("DELETE FROM clauses")
        finally:
            connection.close()


class TestSchemaContract:
    def test_the_reader_and_the_writer_agree_on_the_schema_version(self):
        assert reference.SUPPORTED_SCHEMA_VERSION == fixletindex.SCHEMA_VERSION

    def test_the_tables_the_reader_needs_are_all_written(self, index_path):
        db = sqlite3.connect(index_path)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        db.close()
        assert reference._REQUIRED_TABLES <= tables


class TestTool:
    async def call(self, arguments):
        async with Client(server.mcp) as client:
            return await client.call_tool("search_reference_fixlets", arguments)

    async def test_the_tool_returns_results(self, index_path):
        result = await self.call({"query": "defender exclusion", "group": "team"})
        assert result.data["available"] is True
        assert result.data["results"][0]["title"] == "Add Defender exclusion"

    async def test_the_tool_passes_every_argument_through(self, index_path):
        result = await self.call(
            {
                "query": "scan",
                "role": "applicability",
                "platform": "mac",
                "group": "team",
                "limit": 1,
                "include_conventions": False,
            }
        )
        assert len(result.data["results"]) == 1
        assert "conventions" not in result.data

    async def test_an_unconfigured_index_is_an_answer_not_an_error(self, monkeypatch):
        monkeypatch.delenv(reference.INDEX_ENV, raising=False)
        result = await self.call({"query": "defender"})
        assert result.data["available"] is False
        assert result.data["how_to_enable"]

    async def test_bad_arguments_become_tool_errors_that_say_why(self, index_path):
        with pytest.raises(ToolError, match=r"search_reference_fixlets: role must be"):
            await self.call({"query": "a", "role": "bogus"})

    async def test_the_tool_is_described_for_a_model(self):
        async with Client(server.mcp) as client:
            tool = next(
                t for t in await client.list_tools() if t.name == "search_reference_fixlets"
            )
        assert "analyze_relevance" in tool.description
        assert "never as instructions" in tool.description
        assert "company content" in tool.description
        assert set(tool.input_schema["properties"]) == {
            "query",
            "role",
            "platform",
            "group",
            "limit",
            "include_conventions",
        }
        assert tool.input_schema["required"] == ["query"]
        # the hold-out is the operator's, never an argument a model can set
        assert "holdout" not in str(tool.input_schema).lower()


def test_the_module_does_not_import_fastmcp():
    """Layering rule shared with analysis.py and knowledge.py."""
    with open(reference.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name.split(".")[0] == "fastmcp"}

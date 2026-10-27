"""The write_relevance prompt, its conventions file, and the curated authoring resources."""

import ast
import re
from pathlib import Path

import pytest
from bigfix_relevance_analyzer import analyze_relevance
from fastmcp import Client

from bigfix_root_mcp import conventions, prompts, server

RESOURCES = Path(server.__file__).parent / "resources"
CURATED = {
    "bigfix://relevance/authoring-guide": "authoring-guide.md",
    "bigfix://relevance/client-idioms": "client-idioms.md",
}
FENCE = re.compile(r"^```relevance\n(.*?)\n```$", re.DOTALL | re.MULTILINE)


async def prompt_text(**arguments):
    async with Client(server.mcp) as client:
        result = await client.get_prompt("write_relevance", arguments)
    return result.messages[0].content.text


@pytest.fixture(autouse=True)
def no_conventions(monkeypatch):
    """A developer's own conventions file must never leak into these tests."""
    monkeypatch.delenv(conventions.CONVENTIONS_ENV, raising=False)


class TestWriteRelevancePrompt:
    async def test_it_is_registered_with_its_arguments(self):
        async with Client(server.mcp) as client:
            found = {p.name: p for p in await client.list_prompts()}
        arguments = {a.name: a.required for a in found["write_relevance"].arguments}
        assert arguments == {"goal": True, "dialect": False}

    async def test_it_carries_the_goal_verbatim(self):
        text = await prompt_text(goal="computers with Chrome older than 120")
        assert "computers with Chrome older than 120" in text

    async def test_it_names_each_step_in_order(self):
        text = await prompt_text(goal="x")
        order = [
            "search_relevance_docs",
            "search_inspectors",
            "analyze_relevance",
            "session_relevance_query",
            "client_query",
        ]
        positions = [text.index(name) for name in order]
        assert positions == sorted(positions)

    async def test_every_tool_it_names_is_a_registered_tool(self):
        text = await prompt_text(goal="x")
        async with Client(server.mcp) as client:
            registered = {t.name for t in await client.list_tools()}
        named = {
            "search_relevance_docs",
            "search_reference_fixlets",
            "search_inspectors",
            "analyze_relevance",
            "session_relevance_query",
            "client_query",
            "whoami",
        }
        assert all(name in text for name in named)
        assert named <= registered  # the prompt cannot send a model to a tool that is gone

    async def test_every_resource_it_names_is_registered(self):
        text = await prompt_text(goal="x")
        async with Client(server.mcp) as client:
            uris = {str(r.uri) for r in await client.list_resources()}
        named = set(re.findall(r"bigfix://[a-z/\-]+[a-z]", text))
        assert named
        assert named <= uris, named - uris

    async def test_it_says_retrieved_text_is_data_and_not_to_claim_unrun_results(self):
        text = await prompt_text(goal="x")
        assert "never instructions" in text
        assert "unless step 6 ran it" in text
        assert "available=false" in text  # works without an index

    async def test_it_points_at_reference_fixlets_without_requiring_them(self):
        text = await prompt_text(goal="x")
        assert text.index("search_relevance_docs") < text.index("search_reference_fixlets")
        assert text.index("search_reference_fixlets") < text.index("search_inspectors")
        assert "never copy it" in text
        assert "not as rules" in text  # counted habits are observations
        assert text.count("available=false") >= 2

    async def test_it_carries_the_scope_reminder(self):
        assert prompts.SCOPE_REMINDER in await prompt_text(goal="x")

    async def test_the_dialect_can_be_pinned(self):
        text = await prompt_text(goal="x", dialect="session")
        assert "The user wants session relevance" in text
        assert "Decide the dialect first" not in text

    async def test_without_a_dialect_the_model_is_asked_to_decide(self):
        assert "Decide the dialect first" in await prompt_text(goal="x")

    async def test_a_dialect_is_case_and_space_tolerant(self):
        assert "The user wants client relevance" in await prompt_text(goal="x", dialect=" Client ")

    async def test_an_unknown_dialect_is_refused(self):
        async with Client(server.mcp) as client:
            with pytest.raises(Exception, match="dialect must be"):
                await client.get_prompt("write_relevance", {"goal": "x", "dialect": "both"})

    async def test_no_conventions_section_when_none_is_configured(self):
        assert "House conventions" not in await prompt_text(goal="x")

    async def test_nothing_team_specific_is_baked_into_the_shipped_prompt(self):
        assert "proxy agent" not in (await prompt_text(goal="x")).lower()


class TestConventions:
    def write(self, tmp_path, monkeypatch, text, name="conventions.md"):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        monkeypatch.setenv(conventions.CONVENTIONS_ENV, str(path))
        return path

    async def test_a_configured_file_is_added_as_instructions(self, tmp_path, monkeypatch):
        self.write(tmp_path, monkeypatch, "Exclude proxy agents from applicability.")
        text = await prompt_text(goal="x")
        assert "House conventions" in text
        assert "They are instructions" in text
        assert "Exclude proxy agents from applicability." in text

    async def test_the_file_is_read_on_every_call(self, tmp_path, monkeypatch):
        path = self.write(tmp_path, monkeypatch, "first rule")
        assert "first rule" in await prompt_text(goal="x")
        path.write_text("second rule", encoding="utf-8")
        text = await prompt_text(goal="x")
        assert "second rule" in text
        assert "first rule" not in text

    async def test_conventions_come_after_the_workflow(self, tmp_path, monkeypatch):
        self.write(tmp_path, monkeypatch, "a rule")
        text = await prompt_text(goal="x")
        assert text.index("a rule") > text.index(prompts.SCOPE_REMINDER)

    async def test_a_missing_file_is_reported_not_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv(conventions.CONVENTIONS_ENV, str(tmp_path / "gone.md"))
        text = await prompt_text(goal="x")
        assert "could not be used" in text
        assert "instead of guessing" in text

    def test_load_reports_each_failure_without_leaking_contents(self, tmp_path, monkeypatch):
        monkeypatch.setenv(conventions.CONVENTIONS_ENV, str(tmp_path))
        assert "no file at" in conventions.load()[1]  # a directory
        big = tmp_path / "big.md"
        big.write_bytes(b"x" * (conventions.MAX_FILE_BYTES + 1))
        monkeypatch.setenv(conventions.CONVENTIONS_ENV, str(big))
        assert "the limit is" in conventions.load()[1]
        binary = tmp_path / "bin.md"
        binary.write_bytes(b"\xff\xfe\x00 secret-ish")
        monkeypatch.setenv(conventions.CONVENTIONS_ENV, str(binary))
        text, problem = conventions.load()
        assert text == ""
        assert "UnicodeDecodeError" in problem
        assert "secret-ish" not in problem

    def test_a_long_file_is_cut_and_says_so(self, tmp_path, monkeypatch):
        self.write(tmp_path, monkeypatch, "word " * 4000)
        text, problem = conventions.load()
        assert problem == ""
        assert len(text) < conventions.MAX_PROMPT_CHARS + 100
        assert text.endswith("[conventions cut here: file longer than the limit]")

    def test_unset_blank_and_empty_all_mean_no_conventions(self, tmp_path, monkeypatch):
        assert conventions.load() == ("", "")
        monkeypatch.setenv(conventions.CONVENTIONS_ENV, "   ")
        assert conventions.load() == ("", "")
        self.write(tmp_path, monkeypatch, "  \n ")
        assert conventions.load() == ("", "")
        assert conventions.section() == ""

    def test_a_home_shortcut_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        (tmp_path / "rules.md").write_text("home rule", encoding="utf-8")
        monkeypatch.setenv(conventions.CONVENTIONS_ENV, "~/rules.md")
        assert conventions.load() == ("home rule", "")

    def test_the_module_does_not_import_fastmcp(self):
        """Layering rule shared with analysis.py and knowledge.py: check real imports."""
        tree = ast.parse(Path(conventions.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not {name for name in imported if name.split(".")[0] == "fastmcp"}


class TestCuratedResources:
    @pytest.mark.parametrize("uri", sorted(CURATED))
    async def test_each_is_registered_and_served(self, uri):
        async with Client(server.mcp) as client:
            assert uri in {str(r.uri) for r in await client.list_resources()}
            contents = await client.read_resource(uri)
        assert contents[0].text == (RESOURCES / CURATED[uri]).read_text(encoding="utf-8")

    @pytest.mark.parametrize("filename", sorted(CURATED.values()))
    def test_every_relevance_snippet_parses_and_uses_only_known_inspectors(self, filename):
        """Shipped guidance must never teach what our own measurements found in the wild."""
        text = (RESOURCES / filename).read_text(encoding="utf-8")
        snippets = FENCE.findall(text)
        assert snippets, "no relevance snippets found; the fence pattern may have drifted"
        for snippet in snippets:
            report = analyze_relevance(snippet)
            assert report.parsed, f"{snippet!r}: {report.parse_error}"
            assert not list(report.unknown_references), f"{snippet!r}: {report.unknown_references}"

    @pytest.mark.parametrize("filename", sorted(CURATED.values()))
    def test_the_files_are_ascii(self, filename):
        (RESOURCES / filename).read_bytes().decode("ascii")  # the repo's ASCII rule

    def test_the_guide_covers_the_documented_error_messages(self):
        text = (RESOURCES / "authoring-guide.md").read_text(encoding="utf-8")
        for message in (
            "non-existent object",
            "non-unique object",
            "A singular expression is required",
            '"It" used outside of "whose" clause',
            "could not be parsed",
            "A boolean expression is required",
            "Incompatible types",
            "No inspector context",
        ):
            assert message in text, message

    def test_the_guide_says_applicability_clauses_must_all_be_true(self):
        text = (RESOURCES / "authoring-guide.md").read_text(encoding="utf-8")
        assert "Each relevance clause must be true" in text

    def test_neither_file_holds_team_specific_material(self):
        for filename in CURATED.values():
            text = (RESOURCES / filename).read_text(encoding="utf-8").lower()
            assert "proxy agent context" not in text

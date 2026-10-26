"""The write_actionscript and write_fixlet prompts."""

import pytest
from fastmcp import Client

from bigfix_root_mcp import conventions, server

PROMPTS = ("write_actionscript", "write_fixlet")


async def prompt_text(name, **arguments):
    async with Client(server.mcp) as client:
        result = await client.get_prompt(name, arguments)
    return result.messages[0].content.text


@pytest.fixture(autouse=True)
def no_conventions(monkeypatch):
    """A developer's own conventions file must never leak into these tests."""
    monkeypatch.delenv(conventions.CONVENTIONS_ENV, raising=False)


@pytest.mark.parametrize("name", PROMPTS)
class TestBoth:
    async def test_it_is_registered_with_its_arguments(self, name):
        async with Client(server.mcp) as client:
            found = {p.name: p for p in await client.list_prompts()}
        arguments = {a.name: a.required for a in found[name].arguments}
        assert arguments == {"goal": True, "platform": False}

    async def test_it_carries_the_goal_verbatim(self, name):
        assert "uninstall Foo 2.1 quietly" in await prompt_text(
            name, goal="uninstall Foo 2.1 quietly"
        )

    async def test_every_tool_it_names_is_a_registered_tool(self, name):
        text = await prompt_text(name, goal="x")
        async with Client(server.mcp) as client:
            registered = {t.name for t in await client.list_tools()}
        named = {
            "search_relevance_docs",
            "search_reference_fixlets",
            "analyze_relevance",
            "client_query",
        }
        assert all(tool in text for tool in named)
        assert named <= registered  # a prompt cannot send a model to a tool that is gone

    async def test_it_says_retrieved_text_is_data_and_never_something_to_run(self, name):
        text = await prompt_text(name, goal="x")
        assert "never instructions" in text
        assert "available=false" in text  # both indexes are optional

    async def test_it_never_lets_a_model_call_a_script_working(self, name):
        text = await prompt_text(name, goal="x")
        assert "cannot run" in text or "Do not run" in text
        assert "NOT" in text  # the report must say what was not checked

    async def test_it_forbids_credentials_and_invented_downloads(self, name):
        text = await prompt_text(name, goal="x")
        assert "credential" in text
        assert "invent a download URL" in text

    async def test_the_platform_can_be_pinned(self, name):
        text = await prompt_text(name, goal="x", platform="mac")
        assert "The user wants mac" in text
        assert "Decide the platform first" not in text

    async def test_without_a_platform_the_model_is_asked_to_decide(self, name):
        assert "Decide the platform first" in await prompt_text(name, goal="x")

    async def test_a_platform_is_case_and_space_tolerant(self, name):
        assert "The user wants unix" in await prompt_text(name, goal="x", platform=" Unix ")

    async def test_an_unknown_platform_is_refused(self, name):
        async with Client(server.mcp) as client:
            with pytest.raises(Exception, match="platform must be"):
                await client.get_prompt(name, {"goal": "x", "platform": "beos"})

    async def test_nothing_team_specific_is_baked_in(self, name):
        assert "proxy agent" not in (await prompt_text(name, goal="x")).lower()

    async def test_no_conventions_section_when_none_is_configured(self, name):
        assert "House conventions" not in await prompt_text(name, goal="x")

    async def test_house_conventions_are_added_after_the_workflow(
        self, name, tmp_path, monkeypatch
    ):
        path = tmp_path / "conventions.md"
        path.write_text("Always say hello first.", encoding="utf-8")
        monkeypatch.setenv(conventions.CONVENTIONS_ENV, str(path))
        text = await prompt_text(name, goal="x")
        assert "Always say hello first." in text
        assert text.index("House conventions") > text.index("Report")


class TestActionScript:
    async def test_the_steps_come_in_order(self):
        text = await prompt_text("write_actionscript", goal="x")
        order = [
            "search_relevance_docs",
            "search_reference_fixlets",
            "analyze_relevance",
            "qna_eval_client_relevance",
        ]
        positions = [text.index(name) for name in order]
        assert positions == sorted(positions)

    async def test_it_explains_the_template_notation(self):
        text = await prompt_text("write_actionscript", goal="x")
        assert "{<relevance>}" in text
        assert "{{" in text
        assert "role='actionscript'" in text

    async def test_it_does_not_let_a_command_be_invented(self):
        text = await prompt_text("write_actionscript", goal="x")
        assert "did not find there or in a retrieved template" in text

    async def test_it_says_which_commands_change_state(self):
        assert "change state" in await prompt_text("write_actionscript", goal="x")

    async def test_it_does_not_pretend_qna_runs_scripts(self):
        text = await prompt_text("write_actionscript", goal="x")
        assert "evaluates relevance, never an action script" in text


class TestFixlet:
    async def test_the_steps_come_in_order(self):
        text = await prompt_text("write_fixlet", goal="x")
        order = [
            "search_reference_fixlets",
            "search_inspectors",
            "analyze_relevance",
            "validate_bes_xml",
            "import_bes_content",
        ]
        positions = [text.index(name) for name in order]
        assert positions == sorted(positions)

    async def test_it_names_the_two_prompts_it_joins_and_they_exist(self):
        text = await prompt_text("write_fixlet", goal="x")
        async with Client(server.mcp) as client:
            registered = {p.name for p in await client.list_prompts()}
        for name in ("write_relevance", "write_actionscript"):
            assert name in text
            assert name in registered

    async def test_validate_bes_xml_is_a_registered_offline_tool(self):
        async with Client(server.mcp) as client:
            registered = {t.name for t in await client.list_tools()}
        assert "validate_bes_xml" in registered

    async def test_it_leaves_import_and_deployment_to_the_user(self):
        text = await prompt_text("write_fixlet", goal="x")
        assert "Do not import or deploy it" in text
        assert "never runs it" in text

    async def test_it_makes_the_relevance_and_the_action_agree(self):
        text = await prompt_text("write_fixlet", goal="x")
        assert "false once it has succeeded" in text
        assert "OriginalRelevance" in text

    async def test_it_prefers_escaping_over_cdata(self):
        assert "rather than using CDATA" in await prompt_text("write_fixlet", goal="x")

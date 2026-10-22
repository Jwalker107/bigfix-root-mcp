"""Resources and prompts: reference material the model can pull on demand."""

import pytest
from fastmcp import Client

from bigfix_root_mcp import server

EXPECTED_RESOURCES = {
    "bigfix://relevance/session-cookbook",
    "bigfix://relevance/client-cookbook",
    "bigfix://relevance/authoring-guide",
    "bigfix://relevance/client-idioms",
    "bigfix://guide/tools",
    # served from the bigfix-relevance-analyzer package, not resources/*.md
    "bigfix://relevance/reference/dialects",
    "bigfix://relevance/reference/client-relevance",
    "bigfix://relevance/reference/session-relevance",
}

EXPECTED_PROMPTS = {
    "diagnose_computer",
    "patch_status",
    "find_stale_agents",
    "troubleshoot_relevance",
    "write_relevance",
}


class TestResources:
    async def test_all_resources_are_registered(self):
        async with Client(server.mcp) as client:
            uris = {str(r.uri) for r in await client.list_resources()}
        assert EXPECTED_RESOURCES <= uris

    @pytest.mark.parametrize("uri", sorted(EXPECTED_RESOURCES))
    async def test_each_resource_returns_real_content(self, uri):
        async with Client(server.mcp) as client:
            contents = await client.read_resource(uri)
        text = contents[0].text
        assert len(text) > 500, f"{uri} looks like a stub"
        assert text.lstrip().startswith("#")

    async def test_cookbook_documents_the_missing_limit_operator(self):
        """The single most useful thing in there - guard it against edits."""
        async with Client(server.mcp) as client:
            contents = await client.read_resource("bigfix://relevance/session-cookbook")
        assert "firsts" in contents[0].text


class TestPrompts:
    async def test_all_prompts_are_registered(self):
        async with Client(server.mcp) as client:
            names = {p.name for p in await client.list_prompts()}
        assert EXPECTED_PROMPTS <= names

    async def test_diagnose_computer_mentions_its_argument(self):
        async with Client(server.mcp) as client:
            result = await client.get_prompt("diagnose_computer", {"name_or_id": "WEBSRV01"})
        text = result.messages[0].content.text
        assert "WEBSRV01" in text

    async def test_troubleshoot_relevance_includes_the_failing_expression(self):
        async with Client(server.mcp) as client:
            result = await client.get_prompt(
                "troubleshoot_relevance",
                {
                    "expression": "firsts 10 of bes computers",
                    "error": 'The operator "firsts" is not defined.',
                },
            )
        text = result.messages[0].content.text
        assert "firsts 10 of bes computers" in text
        assert "not defined" in text

    async def test_find_stale_agents_takes_a_day_threshold(self):
        async with Client(server.mcp) as client:
            result = await client.get_prompt("find_stale_agents", {"days": "30"})
        assert "30" in result.messages[0].content.text

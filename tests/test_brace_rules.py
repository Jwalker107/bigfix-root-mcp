"""How braces are written in an action script, from the vendor's own guide.

The guide ("Using Substitution") says: a `{` outside a substitution is written `{{`; a
single `}` outside a substitution is already a literal `}`; inside a substitution a
literal `}` is written `}}`, and a `}` that is not doubled ends the substitution even
inside a string. Doubling every closing brace in a script would put two braces in the
file. These tests keep the template reader, the prompts and the shipped text to that rule.
"""

from pathlib import Path

import pytest
from fastmcp import Client

from bigfix_root_mcp import reference, server
from tools.ingest import actiontemplate as at
from tools.ingest import verdict

REPO = Path(__file__).resolve().parents[1]


class TestVendorExamples:
    """The examples in the guide, word for word."""

    def test_a_left_brace_inside_a_substitution_is_an_ordinary_character(self):
        found = at.parse('echo {"a left brace: {"}')
        assert found.substitutions == ('"a left brace: {"',)
        assert found.text == "echo {<relevance>}"

    def test_a_doubled_right_brace_inside_a_substitution_is_one_literal_brace(self):
        found = at.parse('echo {"{a string inside braces}}"}')
        assert found.substitutions == ('"{a string inside braces}"',)

    def test_a_doubled_left_brace_starts_text_and_a_single_right_brace_is_text(self):
        found = at.parse("appendfile {{ name of operating system } {name of operating system}")
        assert found.text == "appendfile {{ name of operating system } {<relevance>}"
        assert found.substitutions == ("name of operating system",)


class TestClosingBraces:
    def test_a_single_closing_brace_outside_a_substitution_is_text(self):
        found = at.parse("if ($x) {{ Write-Host 1 }")
        assert found.substitutions == ()
        assert found.text == "if ($x) {{ Write-Host 1 }"

    def test_a_doubled_closing_brace_outside_a_substitution_is_two_characters_not_an_escape(self):
        found = at.parse("echo a }} b")
        assert found.substitutions == ()
        assert found.text == "echo a }} b"

    def test_an_undoubled_closing_brace_in_a_string_ends_the_substitution_early(self):
        # the form {"}"} leaves the one character `"` as the relevance: not an expression
        found = at.parse('echo {"}"} done')
        assert found.substitutions == ('"',)
        assert not verdict.analyze_query(found.substitutions[0], "client").parsed

    def test_the_form_that_works_doubles_the_brace_inside_the_substitution(self):
        found = at.parse('echo {"}}"} done')
        assert found.substitutions == ('"}"',)
        assert verdict.analyze_query(found.substitutions[0], "client").parsed

    def test_the_module_says_a_doubled_closing_brace_outside_is_not_an_escape(self):
        assert "two ordinary characters" in (at.__doc__ or "")
        assert "double only the left braces" in (at.__doc__ or "")


WRONG = (
    "`{{` or `}}`",
    "`{{` and `}}` are literal",
    "{{` and `}}` are literal",
    "(`{{`, `}}`)",
)


class TestGuidanceDoesNotTeachTheWrongRule:
    async def test_the_action_script_prompts_say_to_double_only_opening_braces(self):
        async with Client(server.mcp) as client:
            for name in ("write_actionscript", "write_fixlet"):
                result = await client.get_prompt(name, {"goal": "x"})
                text = result.messages[0].content.text
                assert not [bad for bad in WRONG if bad in text], name
                assert "single" in text, name
                assert "`{{`" in text, name

    async def test_the_action_script_prompt_gives_the_whole_rule(self):
        async with Client(server.mcp) as client:
            result = await client.get_prompt("write_actionscript", {"goal": "x"})
        text = result.messages[0].content.text
        assert "double every opening brace and no closing brace" in text
        assert "inside a substitution is a literal `}` written `}}`" in text

    async def test_the_action_script_prompt_sends_the_model_to_the_vendors_rule(self):
        async with Client(server.mcp) as client:
            result = await client.get_prompt("write_actionscript", {"goal": "x"})
        text = result.messages[0].content.text
        assert "search for 'using substitution'" in text
        # and that search finds the guide: its wording is what the rule above is taken from
        assert text.index("search for 'using substitution'") < text.index("3. Look for a similar")

    def test_the_note_that_comes_with_a_script_says_a_single_closing_brace_is_literal(self):
        note = reference._NOTE
        assert not [bad for bad in WRONG if bad in note]
        assert "already literal" in note

    @pytest.mark.parametrize(
        "path",
        [
            "src/bigfix_root_mcp/prompts.py",
            "src/bigfix_root_mcp/reference.py",
            "src/bigfix_root_mcp/server.py",
            "src/bigfix_root_mcp/resources/tool-guide.md",
            "tools/ingest/actiontemplate.py",
            "README.md",
            "docs/design-decisions.md",
            "docs/reference-fixlets.md",
        ],
    )
    def test_no_shipped_text_says_a_closing_brace_must_be_doubled_everywhere(self, path):
        text = (REPO / path).read_text(encoding="utf-8")
        found = [bad for bad in WRONG if bad in text]
        assert not found, f"{path}: {found}"

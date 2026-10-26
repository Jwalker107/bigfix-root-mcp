"""reference.py and search_reference_fixlets for action script templates."""

import pytest
from fastmcp import Client

from bigfix_root_mcp import reference, server
from tools.ingest import fixletbuild, fixletindex, fixlets

SHELL = "application/x-Fixlet-Windows-Shell"


def document(*bodies):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' + "".join(bodies) + "</BES>"
    )


def fixlet_xml(title, description, script, *, option="OriginalRelevance"):
    return (
        f"<Fixlet><Title>{title}</Title><Description>{description}</Description>"
        "<Relevance>windows of operating system</Relevance>"
        f'<DefaultAction><ID>Action1</ID><ActionScript MIMEType="{SHELL}">{script}'
        f'</ActionScript><SuccessCriteria Option="{option}"></SuccessCriteria></DefaultAction>'
        "</Fixlet>"
    )


def write_tree(root, files):
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


DEFENDER = """action uses wow64 redirection false
waithidden powershell.exe Add-MpPreference -ExclusionPath '{pathname of client}'
parameter "Reason"="because"
createfile until END_X
Write-Host hello
END_X"""


@pytest.fixture
def index_path(tmp_path, monkeypatch):
    team = write_tree(
        tmp_path / "team",
        {
            "defender.bes": document(
                fixlet_xml("Add Defender exclusion", "Excludes a folder", DEFENDER)
            ),
            **{
                f"u{n}.bes": document(
                    fixlet_xml(
                        f"Uninstall thing {n}",
                        "Removes it",
                        "waithidden msiexec.exe /X {name of it} /qn",
                    )
                )
                for n in range(6)
            },
        },
    )
    row = {**fixlets.adhoc_row(directory=str(team), group="team"), "id": "team-src"}
    read = fixlets.read_source(row, tmp_path / "raw")
    assert read.error == ""
    built = fixletbuild.assemble([read], min_files=5)
    path = tmp_path / "idx" / "fixlets.sqlite"
    fixletindex.write_index(path, built.data)
    monkeypatch.setenv(reference.INDEX_ENV, str(path))
    monkeypatch.delenv(reference.HOLDOUT_ENV, raising=False)
    return path


class TestSearch:
    def test_a_script_is_found_by_the_words_of_what_it_does(self, index_path):
        found = reference.search("defender exclusion", role="actionscript")
        (top, *_) = found["results"]
        assert top["role"] == "actionscript"
        assert top["title"] == "Add Defender exclusion"

    def test_the_text_inside_a_file_block_is_searchable(self, index_path):
        found = reference.search("Write-Host", role="actionscript")
        assert [r["title"] for r in found["results"]] == ["Add Defender exclusion"]

    def test_a_result_carries_the_template_and_what_it_substitutes(self, index_path):
        found = reference.search("defender exclusion", role="actionscript")
        top = found["results"][0]
        assert "{<relevance>}" in top["template"]
        assert "pathname of client" not in top["template"]
        assert top["substitutions"] == ["pathname of client"]
        assert top["parameters"] == ["Reason"]
        assert top["mime_type"] == SHELL
        assert top["success_criteria"] == "OriginalRelevance"
        assert top["action"] == "Action1"
        assert top["parses"] is True
        assert "relevance" not in top  # a script has a template, not relevance lines

    def test_a_result_is_linked_to_its_item(self, index_path):
        both = reference.search("defender exclusion")["results"]
        items = {r["item"] for r in both if r["title"] == "Add Defender exclusion"}
        roles = {r["role"] for r in both if r["title"] == "Add Defender exclusion"}
        assert len(items) == 1
        assert roles == {"applicability", "actionscript"}

    def test_the_role_narrows(self, index_path):
        roles = {r["role"] for r in reference.search("defender", role="applicability")["results"]}
        assert roles == {"applicability"}

    def test_a_long_template_is_bounded_and_says_how_long_it_was(self, tmp_path, monkeypatch):
        body = "waithidden echo " + "x" * 5000
        tree = write_tree(tmp_path / "t", {"big.bes": document(fixlet_xml("Big one", "d", body))})
        row = {**fixlets.adhoc_row(directory=str(tree)), "id": "big"}
        built = fixletbuild.assemble([fixlets.read_source(row, tmp_path / "raw")])
        path = tmp_path / "big.sqlite"
        fixletindex.write_index(path, built.data)
        monkeypatch.setenv(reference.INDEX_ENV, str(path))
        (top,) = reference.search("big one", role="actionscript")["results"]
        assert len(top["template"]) <= reference.TEMPLATE_CHARS + 4
        assert top["template_chars_total"] > reference.TEMPLATE_CHARS

    def test_many_substitutions_are_listed_to_a_limit(self, tmp_path, monkeypatch):
        body = " ".join("{name of it}" for _ in range(20))
        tree = write_tree(tmp_path / "t", {"m.bes": document(fixlet_xml("Many", "d", body))})
        row = {**fixlets.adhoc_row(directory=str(tree)), "id": "many"}
        built = fixletbuild.assemble([fixlets.read_source(row, tmp_path / "raw")])
        path = tmp_path / "many.sqlite"
        fixletindex.write_index(path, built.data)
        monkeypatch.setenv(reference.INDEX_ENV, str(path))
        (top,) = reference.search("many", role="actionscript")["results"]
        assert len(top["substitutions"]) == reference.SUBSTITUTIONS_SHOWN
        assert top["substitutions_total"] == 20

    def test_the_note_says_a_template_is_to_read_and_not_to_run(self, index_path):
        note = reference.search("defender")["note"]
        assert "never to run" in note
        assert "{<relevance>}" in note


class TestConventions:
    def test_the_commands_scripts_use_come_with_the_results(self, index_path):
        found = reference.search("uninstall thing", role="actionscript")
        habits = [c for c in found["conventions"] if c["role"] == "actionscript"]
        commands = {c["pattern"]: c for c in habits if c["kind"] == "command"}
        assert commands["waithidden"]["items"] == 7
        assert commands["waithidden"]["of"] == 7

    def test_how_scripts_open_is_reported_too(self, index_path):
        found = reference.search("uninstall thing", role="actionscript")
        kinds = {c["kind"] for c in found["conventions"]}
        assert kinds == {"command", "opening"}


class TestHoldOut:
    def test_holding_out_an_items_relevance_holds_out_its_script_too(self, index_path, monkeypatch):
        applicability = reference.search("defender exclusion", role="applicability")["results"][0]
        monkeypatch.setenv(reference.HOLDOUT_ENV, applicability["id"])
        after = reference.search("defender exclusion")["results"]
        assert "Add Defender exclusion" not in [r["title"] for r in after]

    def test_holding_out_a_script_holds_out_its_relevance_too(self, index_path, monkeypatch):
        script = reference.search("defender exclusion", role="actionscript")["results"][0]
        monkeypatch.setenv(reference.HOLDOUT_ENV, script["id"])
        after = reference.search("defender exclusion")["results"]
        assert "Add Defender exclusion" not in [r["title"] for r in after]

    def test_other_items_are_not_held_out(self, index_path, monkeypatch):
        applicability = reference.search("defender exclusion", role="applicability")["results"][0]
        monkeypatch.setenv(reference.HOLDOUT_ENV, applicability["id"])
        assert reference.search("uninstall thing", role="actionscript")["results"]


class TestTool:
    async def test_the_tool_finds_scripts(self, index_path):
        async with Client(server.mcp) as client:
            result = await client.call_tool(
                "search_reference_fixlets", {"query": "defender exclusion", "role": "actionscript"}
            )
        assert result.data["results"][0]["role"] == "actionscript"

    async def test_the_tool_says_scripts_are_not_to_be_run(self):
        async with Client(server.mcp) as client:
            tool = next(
                t for t in await client.list_tools() if t.name == "search_reference_fixlets"
            )
        assert "never to run" in tool.description
        assert "actionscript" in str(tool.input_schema["properties"]["role"])

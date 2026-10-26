"""tools/ingest/actiontemplate.py and the action parsing in bes.py."""

from tools.ingest import actiontemplate as at
from tools.ingest import bes, fixletconventions


class TestSubstitutions:
    def test_a_substitution_becomes_a_marker_and_is_returned(self):
        found = at.parse("waithidden msiexec /X {name of it} /qn")
        assert found.text == "waithidden msiexec /X {<relevance>} /qn"
        assert found.substitutions == ("name of it",)
        assert not found.ambiguous

    def test_several_come_back_in_order(self):
        found = at.parse("copy {a of x} {b of y}")
        assert found.text == "copy {<relevance>} {<relevance>}"
        assert found.substitutions == ("a of x", "b of y")

    def test_a_script_with_none_is_unchanged(self):
        found = at.parse("waithidden cmd.exe /c dir")
        assert found.text == "waithidden cmd.exe /c dir"
        assert found.substitutions == ()

    def test_a_doubled_brace_is_a_literal_and_kept_as_written(self):
        found = at.parse("echo {{literal}} and {real of it}")
        assert found.text == "echo {{literal}} and {<relevance>}"
        assert found.substitutions == ("real of it",)

    def test_a_doubled_close_brace_inside_a_substitution_does_not_end_it(self):
        found = at.parse('echo {(it & "a}}b") of "z"} done')
        assert found.substitutions == ('(it & "a}b") of "z"',)
        assert found.text == "echo {<relevance>} done"

    def test_empty_braces_are_text_not_an_empty_substitution(self):
        found = at.parse("find . -exec rm {} done {real of it}")
        assert found.text == "find . -exec rm {} done {<relevance>}"
        assert found.substitutions == ("real of it",)

    def test_braces_holding_only_spaces_are_text_too(self):
        found = at.parse("echo {  } x")
        assert found.text == "echo {  } x"
        assert found.substitutions == ()

    def test_a_lone_close_brace_outside_a_substitution_is_text(self):
        found = at.parse("echo a } b")
        assert found.text == "echo a } b"
        assert found.substitutions == ()

    def test_an_unclosed_substitution_is_ambiguous_and_nothing_is_guessed(self):
        script = "echo {name of it and more"
        found = at.parse(script)
        assert found.ambiguous
        assert found.text == script
        assert found.substitutions == ()

    def test_whitespace_around_an_expression_is_trimmed(self):
        assert at.parse("x {  name of it  } y").substitutions == ("name of it",)

    def test_a_multiline_substitution_is_one_expression(self):
        found = at.parse("x {name of it\n  & id of it} y")
        assert len(found.substitutions) == 1


class TestParameters:
    def test_declared_parameters_are_found(self):
        found = at.parse('parameter "Version"="1.0"\nparameter "Path"="C:/x"\nwait x')
        assert found.parameters == ("Path", "Version")

    def test_a_parameter_used_in_a_substitution_is_found(self):
        found = at.parse('echo {parameter "Name" of action}')
        assert found.parameters == ("Name",)

    def test_none_gives_an_empty_tuple(self):
        assert at.parse("wait x").parameters == ()


class TestCommandLines:
    def test_blank_lines_are_dropped(self):
        assert at.command_lines("a\n\n  \nb") == ["a", "b"]

    def test_a_file_block_is_collapsed_to_one_line(self):
        script = "createfile until END_X\nGet-Process\nWrite-Host hi\nEND_X\nwait next"
        assert at.command_lines(script) == [
            "createfile until END_X",
            at.FILE_CONTENT,
            "wait next",
        ]

    def test_appendfile_blocks_collapse_too(self):
        lines = at.command_lines("appendfile until X\nbody\nX\ndone")
        assert lines == ["appendfile until X", at.FILE_CONTENT, "done"]

    def test_the_block_keyword_is_case_insensitive(self):
        assert at.FILE_CONTENT in at.command_lines("CreateFile Until Y\nbody\nY")

    def test_an_unterminated_block_swallows_the_rest_rather_than_inventing_commands(self):
        assert at.command_lines("createfile until Z\nbody\nmore") == [
            "createfile until Z",
            at.FILE_CONTENT,
        ]

    def test_commands_are_the_first_words_and_skip_comments_and_metadata(self):
        lines = [
            "// note",
            "waithidden x",
            "WAIT y",
            at.FILE_CONTENT,
            "@mime shell",
            "{<relevance>}",
        ]
        assert at.commands(lines) == {"waithidden", "wait"}


class TestShapeNormalizing:
    def test_urls_and_hashes_collapse_to_placeholders(self):
        digest = "a" * 40
        line = f"add prefetch item name=x sha1:{digest} size:1234 https://example.com/f.msi"
        assert fixletconventions.shape(line) == (
            "add prefetch item name=x sha1:<hash> size:<n> <url>"
        )

    def test_a_marker_survives_so_scripts_that_differ_only_in_relevance_match(self):
        a = fixletconventions.shape("waithidden msiexec /x {<relevance>} /qn")
        b = fixletconventions.shape("WaitHidden msiexec /X {<relevance>} /qn")
        assert a == b

    def test_a_short_hex_word_is_not_a_hash(self):
        assert fixletconventions.shape("echo deadbeef") == "echo deadbeef"


def document(body):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' + body + "</BES>"
    ).encode()


def fixlet(title="T", relevance="true", *actions):
    return (
        f"<Fixlet><Title>{title}</Title><Description>d</Description>"
        f"<Relevance>{relevance}</Relevance>{''.join(actions)}</Fixlet>"
    )


SHELL = "application/x-Fixlet-Windows-Shell"


def default_action(
    script, *, id_="Action1", mime="application/x-Fixlet-Windows-Shell", option=None
):
    criteria = f'<SuccessCriteria Option="{option}"></SuccessCriteria>' if option else ""
    return (
        f'<DefaultAction ID="{id_}"><Description>go</Description>'
        f'<ActionScript MIMEType="{mime}">{script}</ActionScript>{criteria}</DefaultAction>'
    )


def extra_action(script, *, id_="Action2"):
    return (
        f'<Action ID="{id_}"><Description>more</Description>'
        f'<ActionScript MIMEType="{SHELL}">{script}</ActionScript></Action>'
    )


class TestParsingActions:
    def parse(self, *actions, **kwargs):
        return bes.parse_bes(document(fixlet("T", "true", *actions)), "t.bes", **kwargs)[0]

    def test_actions_are_not_read_unless_asked_for(self):
        assert self.parse(default_action("wait x")).actions == []

    def test_the_default_action_and_the_others_are_read_in_that_order(self):
        item = self.parse(extra_action("wait b"), default_action("wait a"), actions=True)
        assert [(a.kind, a.action_id, a.script) for a in item.actions] == [
            ("default", "Action1", "wait a"),
            ("action", "Action2", "wait b"),
        ]

    def test_the_mime_type_and_success_criteria_are_kept(self):
        item = self.parse(
            default_action(
                "x", mime="application/x-Fixlet-Windows-PowerShell", option="OriginalRelevance"
            ),
            actions=True,
        )
        (action,) = item.actions
        assert action.mime_type == "application/x-Fixlet-Windows-PowerShell"
        assert action.success_criteria == "OriginalRelevance"

    def test_no_success_criteria_is_an_empty_string(self):
        (action,) = self.parse(default_action("x"), actions=True).actions
        assert action.success_criteria == ""

    def test_an_action_with_an_empty_script_is_skipped(self):
        assert self.parse(default_action("   "), actions=True).actions == []

    def test_a_very_long_script_is_cut_and_flagged(self):
        (action,) = self.parse(default_action("x" * (bes.SCRIPT_CHARS + 50)), actions=True).actions
        assert len(action.script) == bes.SCRIPT_CHARS
        assert action.truncated

    def test_a_script_at_the_limit_is_not_flagged(self):
        (action,) = self.parse(default_action("x" * bes.SCRIPT_CHARS), actions=True).actions
        assert not action.truncated

    def test_an_analysis_has_no_actions(self):
        body = (
            "<Analysis><Title>A</Title><Relevance>true</Relevance>"
            '<Property Name="p">1</Property></Analysis>'
        )
        (item,) = bes.parse_bes(document(body), "a.bes", actions=True)
        assert item.actions == []


class TestItemId:
    def parse_item(self, title="T", relevance="true"):
        body = fixlet(title, relevance)
        return bes.parse_bes(document(body), "t.bes")[0]

    def test_it_is_stable(self):
        assert bes.item_id(self.parse_item()) == bes.item_id(self.parse_item())

    def test_it_follows_the_title_and_the_applicability(self):
        base = bes.item_id(self.parse_item())
        assert bes.item_id(self.parse_item(title="Other")) != base
        assert bes.item_id(self.parse_item(relevance="false")) != base

    def test_an_analysis_and_all_its_properties_share_one(self):
        body = (
            "<Analysis><Title>A</Title><Relevance>true</Relevance>"
            '<Property Name="one">1</Property><Property Name="two">2</Property></Analysis>'
        )
        (item,) = bes.parse_bes(document(body), "a.bes")
        assert [s.role for s in item.statements].count("property") == 2
        # one id for the item, whichever of its statements is asked about
        assert bes.item_id(item) == bes.item_id(item)

    def test_it_is_twelve_hex_characters(self):
        found = bes.item_id(self.parse_item())
        assert len(found) == 12
        assert all(char in "0123456789abcdef" for char in found)

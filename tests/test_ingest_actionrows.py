"""Action scripts in the reference fixlet index: rows, verdicts, secrets and conventions."""

import sqlite3

from tools.ingest import fixletbuild, fixletindex, fixlets

# Fake credentials are assembled from fragments so no real-looking token sits in this file.
AWS_KEY = "".join(("AK", "IA", "ABCDEFGHIJKLMNOP"))
SHELL = "application/x-Fixlet-Windows-Shell"


def document(*bodies):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' + "".join(bodies) + "</BES>"
    )


def action_fixlet(
    title, script, *, relevance="windows of operating system", option="OriginalRelevance"
):
    criteria = f'<SuccessCriteria Option="{option}"></SuccessCriteria>' if option else ""
    return (
        f"<Fixlet><Title>{title}</Title><Description>Does a job</Description>"
        f"<Relevance>{relevance}</Relevance>"
        f'<DefaultAction ID="Action1"><Description>go</Description>'
        f'<ActionScript MIMEType="{SHELL}">{script}</ActionScript>{criteria}'
        "</DefaultAction></Fixlet>"
    )


def analysis_xml(title):
    return (
        f"<Analysis><Title>{title}</Title><Description>d</Description>"
        '<Relevance>true</Relevance><Property Name="Free">free space of drive</Property>'
        "</Analysis>"
    )


def read_tree(tmp_path, files):
    tree = tmp_path / "tree"
    for name, text in files.items():
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text(text, encoding="utf-8")
    row = fixlets.adhoc_row(directory=str(tree))
    read = fixlets.read_source(row, tmp_path / "raw")
    assert read.error == ""
    return read


def build(tmp_path, files, **kwargs):
    return fixletbuild.assemble([read_tree(tmp_path, files)], **kwargs)


def rel(built):
    return [c for c in built.data.clauses if c.role != "actionscript"]


def action_rows(built):
    return [c for c in built.data.clauses if c.role == "actionscript"]


class TestRows:
    def test_a_script_becomes_a_template_row_linked_to_its_item(self, tmp_path):
        script = "waithidden msiexec.exe /X {name of operating system} /qn"
        built = build(tmp_path, {"a.bes": document(action_fixlet("Remove it", script))})
        (row,) = action_rows(built)
        assert row.template == "waithidden msiexec.exe /X {<relevance>} /qn"
        assert row.substitutions == ("name of operating system",)
        assert row.mime_type == SHELL
        assert row.success_criteria == "OriginalRelevance"
        assert row.action_id == "Action1"
        (applicability,) = rel(built)
        assert row.item_id == applicability.item_id != ""
        assert row.content_id != applicability.content_id

    def test_the_verdict_is_on_the_substitutions(self, tmp_path):
        built = build(tmp_path, {"a.bes": document(action_fixlet("G", "wait {name of it}"))})
        assert action_rows(built)[0].parses is True

    def test_a_substitution_that_does_not_parse_says_so(self, tmp_path):
        script = "wait {this is not (valid}"
        built = build(tmp_path, {"a.bes": document(action_fixlet("B", script))})
        (row,) = action_rows(built)
        assert row.parses is False
        assert row.parse_error

    def test_no_substitution_means_no_verdict_rather_than_a_made_up_one(self, tmp_path):
        built = build(tmp_path, {"a.bes": document(action_fixlet("N", "wait cmd.exe /c dir"))})
        assert action_rows(built)[0].parses is None

    def test_an_ambiguous_script_has_no_verdict_and_stays_unchanged(self, tmp_path):
        script = "wait {name of it and never closed"
        built = build(tmp_path, {"a.bes": document(action_fixlet("A", script))})
        (row,) = action_rows(built)
        assert row.parses is None
        assert row.template == script
        assert row.substitutions == ()

    def test_parameters_are_recorded(self, tmp_path):
        script = """parameter "Version"="1.0"
wait x"""
        built = build(tmp_path, {"a.bes": document(action_fixlet("P", script))})
        assert action_rows(built)[0].parameters == ("Version",)

    def test_the_command_lines_collapse_a_file_block_and_add_metadata(self, tmp_path):
        script = """createfile until END_X
Get-Process
END_X
waithidden powershell x"""
        built = build(tmp_path, {"a.bes": document(action_fixlet("F", script))})
        (row,) = action_rows(built)
        assert "Get-Process" not in " ".join(row.relevance)
        assert row.relevance[:3] == (
            "createfile until END_X",
            "<file content>",
            "waithidden powershell x",
        )
        assert row.relevance[-2:] == ("@mime x-fixlet-windows-shell", "@success originalrelevance")
        assert "Get-Process" in row.template  # the template keeps the whole script

    def test_the_text_of_a_file_block_is_still_searchable(self, tmp_path):
        script = """createfile until END_X
Add-MpPreference
END_X"""
        built = build(tmp_path, {"a.bes": document(action_fixlet("F", script))})
        out = tmp_path / "f.sqlite"
        fixletindex.write_index(out, built.data)
        found = (
            sqlite3.connect(out)
            .execute(
                "SELECT role FROM clauses WHERE id IN"
                " (SELECT rowid FROM clauses_fts WHERE clauses_fts MATCH 'MpPreference')"
            )
            .fetchall()
        )
        assert ("actionscript",) in found

    def test_a_parameterized_script_gets_the_metadata_line(self, tmp_path):
        built = build(tmp_path, {"a.bes": document(action_fixlet("P", 'parameter "x"="1"'))})
        assert "@parameterized" in action_rows(built)[0].relevance

    def test_no_success_criteria_is_recorded_as_none(self, tmp_path):
        built = build(tmp_path, {"a.bes": document(action_fixlet("S", "wait x", option=""))})
        assert "@success none" in action_rows(built)[0].relevance

    def test_each_action_of_an_item_is_its_own_row(self, tmp_path):
        body = (
            "<Task><Title>Two</Title><Description>d</Description><Relevance>true</Relevance>"
            f'<DefaultAction ID="A1"><ActionScript MIMEType="{SHELL}">wait one'
            "</ActionScript></DefaultAction>"
            f'<Action ID="A2"><ActionScript MIMEType="{SHELL}">wait two'
            "</ActionScript></Action></Task>"
        )
        rows = action_rows(build(tmp_path, {"t.bes": document(body)}))
        assert [r.action_id for r in rows] == ["A1", "A2"]
        assert len({r.content_id for r in rows}) == 2

    def test_a_cut_script_is_flagged(self, tmp_path):
        built = build(tmp_path, {"a.bes": document(action_fixlet("L", "x" * 70000))})
        assert action_rows(built)[0].script_truncated

    def test_an_analysis_adds_no_action_rows(self, tmp_path):
        assert action_rows(build(tmp_path, {"d.bes": document(analysis_xml("Disk"))})) == []

    def test_an_analysis_and_its_properties_share_one_item_id(self, tmp_path):
        built = build(tmp_path, {"d.bes": document(analysis_xml("Disk"))})
        assert len(built.data.clauses) == 2
        assert len({c.item_id for c in built.data.clauses}) == 1


class TestSecrets:
    def test_a_credential_in_a_script_drops_only_that_row(self, tmp_path):
        built = build(tmp_path, {"a.bes": document(action_fixlet("Leaky", f"set key {AWS_KEY}"))})
        assert action_rows(built) == []
        assert len(rel(built)) == 1  # the relevance stays
        assert built.dropped == 1
        assert any(e.field == "script" and e.dropped_clause for e in built.exclusions)
        out = tmp_path / "f.sqlite"
        fixletindex.write_index(out, built.data)
        assert AWS_KEY.encode() not in out.read_bytes()
        assert AWS_KEY not in fixletbuild.format_report(built)

    def test_the_report_says_a_script_was_dropped(self, tmp_path):
        built = build(tmp_path, {"a.bes": document(action_fixlet("Leaky", f"set key {AWS_KEY}"))})
        assert "script dropped" in fixletbuild.format_report(built)

    def test_a_credential_inside_a_file_block_is_still_found(self, tmp_path):
        script = f"""createfile until E
token = {AWS_KEY}
E"""
        built = build(tmp_path, {"a.bes": document(action_fixlet("Hidden", script))})
        assert action_rows(built) == []


class TestConventions:
    def test_commands_and_metadata_are_counted_per_group(self, tmp_path):
        files = {
            f"f{n}.bes": document(action_fixlet(f"F{n}", f"waithidden cmd /c {n}"))
            for n in range(6)
        }
        built = build(tmp_path, files, min_files=5)
        rows = [c for c in built.data.conventions if c.role == "actionscript"]
        commands = {c.pattern: c for c in rows if c.kind == "command" and c.platform == "all"}
        assert commands["waithidden"].files == 6
        lines = {c.pattern for c in rows if c.kind == "line" and c.platform == "all"}
        assert "@mime x-fixlet-windows-shell" in lines
        assert "@success originalrelevance" in lines

    def test_file_block_text_is_never_counted_as_a_habit(self, tmp_path):
        script = """createfile until E
Get-Process
E
wait x"""
        files = {f"f{n}.bes": document(action_fixlet(f"F{n}", script)) for n in range(6)}
        built = build(tmp_path, files, min_files=1, min_share=0.0)
        patterns = {c.pattern for c in built.data.conventions if c.role == "actionscript"}
        assert not any("get-process" in p for p in patterns)
        assert "createfile" in patterns  # the command itself is

    def test_a_conjunct_is_not_counted_for_a_script(self, tmp_path):
        files = {f"f{n}.bes": document(action_fixlet(f"F{n}", "wait x")) for n in range(6)}
        built = build(tmp_path, files, min_files=1, min_share=0.0)
        kinds = {c.kind for c in built.data.conventions if c.role == "actionscript"}
        assert "conjunct" not in kinds
        assert {"line", "command", "opening", "shape"} <= kinds

    def test_the_index_accepts_the_new_kinds(self, tmp_path):
        files = {f"f{n}.bes": document(action_fixlet(f"F{n}", "wait x")) for n in range(6)}
        built = build(tmp_path, files, min_files=1, min_share=0.0)
        out = tmp_path / "f.sqlite"
        fixletindex.write_index(out, built.data)
        db = sqlite3.connect(out)
        kinds = {row[0] for row in db.execute("SELECT kind FROM conventions")}
        assert "command" in kinds

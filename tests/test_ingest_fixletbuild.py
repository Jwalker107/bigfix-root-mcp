"""tools/ingest/fixletindex.py and fixletbuild.py: the reference fixlet index."""

import json
import sqlite3

import pytest

from tools.ingest import bes, fixletbuild, fixletindex, fixlets, scan

# Fake credentials are assembled from fragments so no real-looking token sits in this file.
AWS_KEY = "".join(("AK", "IA", "ABCDEFGHIJKLMNOP"))
EMAIL = "".join(("someone", "@", "internal-corp", ".net"))


def document(*bodies):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' + "".join(bodies) + "</BES>"
    )


def fixlet(title="Install Foo", relevance=("windows of operating system",), **parts):
    description = parts.get("description", "Installs Foo.")
    script = parts.get("script", "run foo")
    rel = "".join(f"<Relevance>{r}</Relevance>" for r in relevance)
    return (
        f"<Fixlet><Title>{title}</Title><Description>{description}</Description>{rel}"
        f"<Action><ActionScript>{script}</ActionScript></Action></Fixlet>"
    )


def analysis(title="Disk", properties=(("Free", "free space of drive"),), relevance=("true",)):
    rel = "".join(f"<Relevance>{r}</Relevance>" for r in relevance)
    props = "".join(f'<Property Name="{n}">{r}</Property>' for n, r in properties)
    return f"<Analysis><Title>{title}</Title><Description>d</Description>{rel}{props}</Analysis>"


def read_tree(tmp_path, files):
    tree = tmp_path / "tree"
    for name, text in files.items():
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text(text, encoding="utf-8")
    row = fixlets.adhoc_row(directory=str(tree))
    read = fixlets.read_source(row, tmp_path / "raw")
    assert read.error == ""
    return read


def rel(built):
    """The relevance rows only: every fixlet here also has an action script row."""
    return [c for c in built.data.clauses if c.role != "actionscript"]


def build(tmp_path, files, allowed=None):
    return fixletbuild.assemble([read_tree(tmp_path, files)], allowed)


class TestAssemble:
    def test_a_fixlet_gives_one_applicability_clause_holding_all_its_lines(self, tmp_path):
        built = build(
            tmp_path,
            {"foo.bes": document(fixlet(relevance=("windows of operating system", "true")))},
        )
        (clause,) = rel(built)
        assert clause.role == "applicability"
        assert clause.relevance == ("windows of operating system", "true")
        assert clause.file_name == "foo"
        assert clause.title == "Install Foo"
        assert clause.action_excerpt == "run foo"
        assert clause.dialect == "client"

    def test_an_analysis_gives_one_clause_per_property(self, tmp_path):
        files = {
            "d.bes": document(analysis(properties=(("Free", "free space of drive"), ("Used", "1"))))
        }
        built = build(tmp_path, files)
        roles = sorted((c.role, c.property_name) for c in rel(built))
        assert roles == [("applicability", ""), ("property", "Free"), ("property", "Used")]

    def test_the_analyzer_verdict_is_recorded(self, tmp_path):
        built = build(
            tmp_path,
            {
                "ok.bes": document(fixlet("Good", ("true",))),
                "bad.bes": document(fixlet("Bad", ("this is not (relevance",))),
            },
        )
        by_title = {c.title: c for c in rel(built)}
        assert by_title["Good"].parses is True
        assert by_title["Bad"].parses is False
        assert by_title["Bad"].parse_error

    def test_the_content_id_matches_the_question_collectors(self, tmp_path):
        built = build(tmp_path, {"foo.bes": document(fixlet())})
        item = bes.extract(tmp_path / "tree").items[0]
        expected = bes.content_id(item.kind, item.title, "applicability", item.statements)
        assert rel(built)[0].content_id == expected

    def test_each_read_is_recorded_with_when_and_what(self, tmp_path):
        built = build(tmp_path, {"foo.bes": document(fixlet())})
        (read,) = built.data.reads
        assert read.kind == "dir"
        assert read.read_at.endswith("Z")
        assert len(read.tree_sha256) == 64
        assert (read.files, read.items) == (1, 1)

    def test_an_unreadable_source_adds_nothing(self):
        built = fixletbuild.assemble([fixlets.Read("x", error="boom")])
        assert rel(built) == []
        assert built.data.reads == []


class TestSecrets:
    def test_a_credential_in_the_relevance_drops_the_clause(self, tmp_path):
        built = build(
            tmp_path, {"a.bes": document(fixlet(relevance=(f'name of it = "{AWS_KEY}"',)))}
        )
        assert rel(built) == []
        assert built.dropped == 1
        assert built.exclusions[0].dropped_clause

    def test_a_credential_in_the_action_script_leaves_only_the_excerpt_out(self, tmp_path):
        built = build(tmp_path, {"a.bes": document(fixlet(script=f"set key {AWS_KEY}"))})
        (clause,) = rel(built)
        assert clause.action_excerpt == ""
        assert clause.excluded == ("action",)
        assert clause.title == "Install Foo"  # the rest of the clause stays

    def test_a_credential_in_the_description_leaves_only_it_out(self, tmp_path):
        built = build(tmp_path, {"a.bes": document(fixlet(description=f"key {AWS_KEY}"))})
        (clause,) = rel(built)
        assert (clause.description, clause.excluded) == ("", ("description",))
        assert clause.action_excerpt == "run foo"

    def test_the_report_never_contains_the_matched_text(self, tmp_path):
        built = build(tmp_path, {"a.bes": document(fixlet(script=f"set key {AWS_KEY}"))})
        report = fixletbuild.format_report(built)
        assert AWS_KEY not in report
        assert "action left out" in report
        assert "a.bes" in report

    def test_nothing_the_scan_blocked_is_stored(self, tmp_path):
        built = build(tmp_path, {"a.bes": document(fixlet(script=f"set key {AWS_KEY}"))})
        out = tmp_path / "idx.sqlite"
        fixletindex.write_index(out, built.data)
        assert AWS_KEY.encode() not in out.read_bytes()

    def test_a_warning_is_counted_and_the_text_kept(self, tmp_path):
        built = build(tmp_path, {"a.bes": document(fixlet(description=f"ask {EMAIL}"))})
        (clause,) = rel(built)
        assert EMAIL in clause.description
        assert built.warnings["email-address"] == 1

    def test_the_allowlist_suppresses_a_known_false_positive(self, tmp_path):
        files = {"a.bes": document(fixlet(script=f"set key {AWS_KEY}"))}
        allowed = {("dir-x", "a.bes", "aws-access-key")}
        row = fixlets.adhoc_row(directory=str(tmp_path / "tree"))
        allowed = {(row["id"], "a.bes", "aws-access-key")}
        built = build(tmp_path, files, allowed)
        assert built.data.clauses[0].excluded == ()
        assert built.exclusions == []

    def test_finding_rules_are_the_documentation_builds(self):
        assert "aws-access-key" in scan.RULE_NAMES


class TestIndex:
    @pytest.fixture
    def index_path(self, tmp_path):
        files = {
            "foo.bes": document(fixlet(title="Install Foo", description="Deploys the Foo agent")),
            "d.bes": document(analysis(title="Disk space")),
        }
        built = build(tmp_path, files)
        out = tmp_path / "out" / "fixlets.sqlite"
        fixletindex.write_index(out, built.data)
        return out

    def test_the_file_says_it_is_confidential_and_how_it_was_built(self, index_path):
        meta = dict(sqlite3.connect(index_path).execute("SELECT key, value FROM meta"))
        assert meta["schema_version"] == str(fixletindex.SCHEMA_VERSION)
        assert "do not distribute" in meta["confidential"]
        # foo.bes: its applicability and its action script; d.bes: the analysis applicability and
        # its property
        assert meta["clause_count"] == "4"
        assert meta["analyzer_version"]

    def test_user_version_is_set_for_the_reader_to_check(self, index_path):
        version = sqlite3.connect(index_path).execute("PRAGMA user_version").fetchone()[0]
        assert version == fixletindex.SCHEMA_VERSION

    def test_the_purpose_text_is_searchable(self, index_path):
        rows = sqlite3.connect(index_path).execute(
            "SELECT title FROM clauses WHERE id IN"
            " (SELECT rowid FROM clauses_fts WHERE clauses_fts MATCH 'deploy')"
        )
        assert {r[0] for r in rows} == {"Install Foo"}

    def test_the_relevance_is_searchable_as_text(self, index_path):
        rows = sqlite3.connect(index_path).execute(
            "SELECT title FROM clauses WHERE id IN"
            " (SELECT rowid FROM clauses_fts WHERE clauses_fts MATCH 'drive')"
        )
        assert {r[0] for r in rows} == {"Disk space"}

    def test_exact_relevance_lines_survive_as_json(self, index_path):
        row = (
            sqlite3.connect(index_path)
            .execute("SELECT relevance FROM clauses WHERE title = 'Install Foo'")
            .fetchone()
        )
        assert json.loads(row[0]) == ["windows of operating system"]

    def test_a_clause_for_an_unknown_source_is_refused(self, tmp_path):
        data = fixletindex.FixletIndexData(
            clauses=[
                fixletindex.ClauseRow(
                    "nope", "a.bes", "a", "id", "Fixlet", "applicability", "", "t", "", "", ("x",)
                )
            ]
        )
        with pytest.raises(ValueError, match="unknown source"):
            fixletindex.write_index(tmp_path / "i.sqlite", data)
        assert not (tmp_path / "i.sqlite").exists()
        assert not (tmp_path / "i.sqlite.tmp").exists()


class TestCommandLine:
    def run(self, capsys, tmp_path, *args):
        out = tmp_path / "fixlets.sqlite"
        log = tmp_path / "reads.jsonl"
        code = fixletbuild.main(
            [
                *args,
                "--out",
                str(out),
                "--reads-log",
                str(log),
                "--raw-dir",
                str(tmp_path / "raw"),
                "--scan-allow",
                str(tmp_path / "none.csv"),
            ]
        )
        return code, capsys.readouterr().out, out, log

    def tree(self, tmp_path, files=None):
        files = files or {"foo.bes": document(fixlet())}
        tree = tmp_path / "tree"
        for name, text in files.items():
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_text(text, encoding="utf-8")
        return tree

    def test_a_directory_is_built_and_its_read_recorded(self, capsys, tmp_path):
        tree = self.tree(tmp_path)
        code, out_text, out, log = self.run(capsys, tmp_path, "--dir", str(tree))
        assert code == 0
        assert out.exists()
        assert "company content" in out_text
        assert json.loads(log.read_text(encoding="utf-8"))["files"] == 1

    def test_a_dry_run_writes_neither_index_nor_record(self, capsys, tmp_path):
        tree = self.tree(tmp_path)
        code, out_text, out, log = self.run(capsys, tmp_path, "--dir", str(tree), "--dry-run")
        assert code == 0
        assert "nothing written" in out_text
        assert not out.exists()
        assert not log.exists()

    def test_a_source_that_cannot_be_read_stops_the_build(self, capsys, tmp_path):
        code, out_text, out, log = self.run(capsys, tmp_path, "--dir", str(tmp_path / "absent"))
        assert code == 1
        assert "not built" in out_text
        assert not out.exists()
        assert not log.exists()

    def test_nothing_to_index_is_a_problem_not_an_empty_index(self, capsys, tmp_path):
        tree = self.tree(tmp_path, {"readme.txt": "no fixlets here"})
        code, out_text, out, _log = self.run(capsys, tmp_path, "--dir", str(tree))
        assert code == 1
        assert "nothing to index" in out_text
        assert not out.exists()

    def test_listed_sources_are_built_by_id(self, capsys, tmp_path):
        tree = self.tree(tmp_path)
        sources = tmp_path / "s.csv"
        sources.write_text(
            f"id,kind,location,branch,commit,subpath,notes\nmine,dir,{tree},,,,\n",
            encoding="utf-8",
        )
        code, _text, out, _log = self.run(
            capsys, tmp_path, "--source", "mine", "--sources", str(sources)
        )
        assert code == 0
        assert out.exists()

    def test_an_unknown_source_id_is_a_user_error(self, capsys, tmp_path):
        sources = tmp_path / "s.csv"
        sources.write_text("id,kind,location\nmine,dir,/x\n", encoding="utf-8")
        code, _text, out, _log = self.run(
            capsys, tmp_path, "--source", "nope", "--sources", str(sources)
        )
        assert code == 2
        assert not out.exists()

    def test_the_report_never_prints_file_contents(self, capsys, tmp_path):
        tree = self.tree(
            tmp_path, {"a.bes": document(fixlet(relevance=("name of it = PLANTEDTEXT",)))}
        )
        _code, out_text, _out, _log = self.run(capsys, tmp_path, "--dir", str(tree))
        assert "PLANTEDTEXT" not in out_text

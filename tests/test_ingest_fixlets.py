"""tools/ingest/fixlets.py: reading reference fixlets from a directory or a git repository."""

import json
import shutil
from datetime import UTC, datetime

import pytest

from tests.ingest_git import commit_files, git, make_repo
from tools.ingest import fixlets

NOW = datetime(2026, 10, 22, 12, 30, 5, tzinfo=UTC)
FILE = ("file",)  # tests fetch from local file:// repositories
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def clock():
    return NOW


def bes_text(relevance="true", title="Reference"):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<BES><Fixlet><Title>{title}</Title><Relevance>{relevance}</Relevance></Fixlet></BES>"
    )


def write_tree(root, files):
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    return root


def row(**fields):
    base = {
        "id": "ref",
        "kind": "dir",
        "location": "",
        "branch": "",
        "commit": "",
        "subpath": "",
        "notes": "",
    }
    return {**base, **fields}


def write_sources(path, *lines):
    path.write_text("id,kind,location,branch,commit,subpath,notes\n" + "\n".join(lines) + "\n")
    return path


class TestSourceList:
    def test_valid_rows_are_read(self, tmp_path):
        path = write_sources(
            tmp_path / "s.csv",
            "a,dir,C:/ref/fixlets,,,,team tree",
            "b,git,https://example.com/org/repo.git,main,,sub/dir,",
        )
        rows = fixlets.read_sources(path)
        assert [r["id"] for r in rows] == ["a", "b"]
        assert rows[1]["subpath"] == "sub/dir"

    @pytest.mark.parametrize(
        ("line", "message"),
        [
            ("a b,dir,/x,,,,", "invalid id"),
            ("a,svn,/x,,,,", "kind must be"),
            ("a,dir,,,,,", "location is empty"),
            ("a,dir,/x,,,../up,", "subpath"),
            ("a,dir,/x,,,/abs,", "subpath"),
            ("a,dir,/x,,,C:/abs,", "subpath"),
            ("a,git,-oops,,,,", "invalid git location"),
            ("a,git,https://user:pw@example.com/r.git,,,,", "password"),
            ("a,git,https://user@example.com/r.git,,,,", "credentials"),
            ("a,git,https://example.com/r.git,,abc123,,", "40-character"),
        ],
    )
    def test_a_bad_row_is_refused_with_a_reason(self, tmp_path, line, message):
        with pytest.raises(ValueError, match=message):
            fixlets.read_sources(write_sources(tmp_path / "s.csv", line))

    def test_a_login_name_is_allowed_for_ssh_only(self, tmp_path):
        rows = fixlets.read_sources(
            write_sources(tmp_path / "s.csv", "a,git,ssh://git@example.com/org/r.git,,,,")
        )
        assert rows[0]["location"].startswith("ssh://git@")

    def test_a_duplicate_id_is_refused(self, tmp_path):
        path = write_sources(tmp_path / "s.csv", "a,dir,/x,,,,", "a,dir,/y,,,,")
        with pytest.raises(ValueError, match="duplicate id"):
            fixlets.read_sources(path)

    def test_a_missing_column_is_refused(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text("id,kind\na,dir\n")
        with pytest.raises(ValueError, match="missing required column"):
            fixlets.read_sources(path)

    def test_an_adhoc_id_is_stable_for_one_location(self, tmp_path):
        first = fixlets.adhoc_row(directory=str(tmp_path))
        again = fixlets.adhoc_row(directory=str(tmp_path / "." / ""))
        assert first["id"] == again["id"]
        assert first["id"].startswith("dir-")
        assert fixlets.adhoc_row(repo="https://example.com/r.git")["id"].startswith("git-")


class TestDirectory:
    def read(self, root, **fields):
        return fixlets.read_source(row(location=str(root), **fields), root / "raw", clock=clock)

    def test_the_record_says_when_and_what_was_read(self, tmp_path):
        tree = write_tree(tmp_path / "tree", {"a.BES": bes_text(), "sub/b.bes": bes_text("false")})
        read = self.read(tree)
        assert read.error == ""
        record = read.record
        assert record["read_at"] == "2026-10-22T12:30:05Z"
        assert record["kind"] == "dir"
        assert (record["files"], record["items"], record["unparsable_files"]) == (2, 2, 0)
        assert record["commit"] == ""
        assert len(record["tree_sha256"]) == 64

    def test_the_fingerprint_is_stable_and_follows_the_content(self, tmp_path):
        tree = write_tree(tmp_path / "tree", {"a.bes": bes_text()})
        first = self.read(tree).record["tree_sha256"]
        assert self.read(tree).record["tree_sha256"] == first
        (tree / "a.bes").write_text(bes_text("false"), encoding="utf-8")
        assert self.read(tree).record["tree_sha256"] != first

    def test_a_rename_changes_the_fingerprint(self, tmp_path):
        tree = write_tree(tmp_path / "tree", {"a.bes": bes_text()})
        first = self.read(tree).record["tree_sha256"]
        (tree / "a.bes").rename(tree / "b.bes")
        assert self.read(tree).record["tree_sha256"] != first

    def test_other_files_do_not_change_the_fingerprint(self, tmp_path):
        tree = write_tree(tmp_path / "tree", {"a.bes": bes_text()})
        first = self.read(tree).record["tree_sha256"]
        (tree / "notes.txt").write_text("hello", encoding="utf-8")
        assert self.read(tree).record["tree_sha256"] == first

    def test_nothing_is_written_under_the_source(self, tmp_path):
        tree = write_tree(tmp_path / "tree", {"a.bes": bes_text()})
        before = sorted(p.name for p in tree.rglob("*"))
        self.read(tree)
        assert sorted(p.name for p in tree.rglob("*")) == before

    def test_a_missing_directory_is_an_error_not_a_crash(self, tmp_path):
        read = self.read(tmp_path / "absent")
        assert "not a directory" in read.error
        assert read.record == {}

    def test_a_subpath_limits_what_is_read(self, tmp_path):
        tree = write_tree(tmp_path / "tree", {"keep/a.bes": bes_text(), "other/b.bes": bes_text()})
        assert self.read(tree, subpath="keep").record["files"] == 1

    def test_a_subpath_that_is_not_a_directory_is_an_error(self, tmp_path):
        tree = write_tree(tmp_path / "tree", {"a.bes": bes_text()})
        assert "no such directory" in self.read(tree, subpath="absent").error

    def test_an_invalid_row_is_an_error(self, tmp_path):
        read = fixlets.read_source(row(kind="svn"), tmp_path, clock=clock)
        assert "kind must be" in read.error

    def test_the_report_names_counts_never_contents(self, tmp_path):
        tree = write_tree(tmp_path / "tree", {"a.bes": bes_text("name of it = PLANTEDTEXT")})
        report = fixlets.format_read(self.read(tree))
        assert "PLANTEDTEXT" not in report
        assert "read at: 2026-10-22T12:30:05Z" in report


@needs_git
class TestRepository:
    @pytest.fixture
    def repo(self, tmp_path):
        path = make_repo(tmp_path / "upstream")
        first = commit_files(path, {"fix/a.bes": bes_text("true", "A")}, "first")
        second = commit_files(path, {"fix/b.bes": bes_text("false", "B")}, "second")
        return path, first, second

    def read(self, path, tmp_path, **fields):
        base = row(kind="git", location=path.as_uri(), **fields)
        return fixlets.read_source(base, tmp_path / "raw", allow_schemes=FILE, clock=clock)

    def test_a_blank_commit_reads_the_branch_tip_and_records_it(self, repo, tmp_path):
        path, _first, second = repo
        read = self.read(path, tmp_path)
        assert read.error == ""
        record = read.record
        assert record["commit"] == second
        assert record["commit_date"]  # git's own date for that commit
        assert record["read_at"] == "2026-10-22T12:30:05Z"
        assert (record["files"], record["items"]) == (2, 2)

    def test_a_pinned_commit_reads_that_commit(self, repo, tmp_path):
        path, first, _second = repo
        read = self.read(path, tmp_path, commit=first)
        assert read.record["commit"] == first
        assert read.record["files"] == 1

    def test_a_subpath_limits_what_is_read(self, repo, tmp_path):
        path, _first, _second = repo
        commit_files(path, {"elsewhere/c.bes": bes_text()}, "third")
        assert self.read(path, tmp_path, subpath="fix").record["files"] == 2

    def test_a_second_read_of_the_same_commit_gives_the_same_fingerprint(self, repo, tmp_path):
        path, _first, _second = repo
        a = self.read(path, tmp_path).record
        b = self.read(path, tmp_path).record
        assert a["tree_sha256"] == b["tree_sha256"]
        assert a["commit"] == b["commit"]

    def test_a_new_upstream_commit_is_picked_up(self, repo, tmp_path):
        path, _first, second = repo
        before = self.read(path, tmp_path).record
        third = commit_files(path, {"fix/c.bes": bes_text("true", "C")}, "third")
        after = self.read(path, tmp_path).record
        assert (before["commit"], after["commit"]) == (second, third)
        assert before["tree_sha256"] != after["tree_sha256"]

    def test_a_branch_that_does_not_exist_is_an_error(self, repo, tmp_path):
        path, _first, _second = repo
        assert self.read(path, tmp_path, branch="nope").error

    def test_a_branch_that_could_be_an_option_is_refused(self, repo, tmp_path):
        path, _first, _second = repo
        assert "invalid branch" in self.read(path, tmp_path, branch="--upload-pack=x").error

    def test_a_scheme_that_is_not_allowed_is_an_error(self, repo, tmp_path):
        path, _first, _second = repo
        base = row(kind="git", location=path.as_uri())
        read = fixlets.read_source(base, tmp_path / "raw", clock=clock)  # https only
        assert read.error

    def test_a_dry_run_fetches_and_records_nothing(self, repo, tmp_path):
        path, _first, second = repo
        base = row(kind="git", location=path.as_uri())
        read = fixlets.read_source(
            base, tmp_path / "raw", allow_schemes=FILE, clock=clock, dry_run=True
        )
        assert read.record == {}
        assert second[:12] in read.note
        assert not (tmp_path / "raw").exists()

    def test_a_checkout_with_local_changes_is_not_touched(self, repo, tmp_path):
        path, _first, _second = repo
        self.read(path, tmp_path)
        checkout = tmp_path / "raw" / "ref"
        (checkout / "fix" / "a.bes").write_text("tampered", encoding="utf-8")
        assert "local modifications" in self.read(path, tmp_path).error
        assert (checkout / "fix" / "a.bes").read_text(encoding="utf-8") == "tampered"
        assert git(checkout, "rev-parse", "HEAD")


class TestReadLog:
    def test_records_are_appended_never_rewritten(self, tmp_path):
        log = tmp_path / "index" / "reads.jsonl"
        fixlets.append_read({"source_id": "a", "read_at": "t1"}, log)
        fixlets.append_read({"source_id": "a", "read_at": "t2"}, log)
        lines = log.read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["read_at"] for line in lines] == ["t1", "t2"]

    def test_the_log_is_ascii(self, tmp_path):
        log = tmp_path / "reads.jsonl"
        fixlets.append_read({"location": "na" + chr(0xEF) + "ve"}, log)
        assert log.read_bytes().isascii()


class TestCommandLine:
    def run(self, capsys, tmp_path, *args):
        log = tmp_path / "reads.jsonl"
        code = fixlets.main([*args, "--raw-dir", str(tmp_path / "raw"), "--reads-log", str(log)])
        return code, capsys.readouterr().out, log

    def test_a_directory_is_read_and_recorded(self, capsys, tmp_path):
        tree = write_tree(tmp_path / "tree", {"a.bes": bes_text()})
        code, out, log = self.run(capsys, tmp_path, "--dir", str(tree))
        assert code == 0
        assert "read at:" in out
        assert json.loads(log.read_text(encoding="utf-8"))["files"] == 1

    def test_a_dry_run_writes_no_record(self, capsys, tmp_path):
        tree = write_tree(tmp_path / "tree", {"a.bes": bes_text()})
        code, _out, log = self.run(capsys, tmp_path, "--dir", str(tree), "--dry-run")
        assert code == 0
        assert not log.exists()

    def test_a_listed_source_is_read_by_id(self, capsys, tmp_path):
        tree = write_tree(tmp_path / "tree", {"a.bes": bes_text()})
        sources = write_sources(tmp_path / "s.csv", f"mine,dir,{tree},,,,")
        code, out, _log = self.run(capsys, tmp_path, "--source", "mine", "--sources", str(sources))
        assert code == 0
        assert "mine" in out

    def test_an_unknown_source_id_is_a_user_error(self, capsys, tmp_path):
        sources = write_sources(tmp_path / "s.csv", "mine,dir,/x,,,,")
        code, _out, _log = self.run(capsys, tmp_path, "--source", "nope", "--sources", str(sources))
        assert code == 2

    def test_a_missing_directory_exits_with_failure(self, capsys, tmp_path):
        code, out, log = self.run(capsys, tmp_path, "--dir", str(tmp_path / "absent"))
        assert code == 1
        assert "error" in out
        assert not log.exists()

    def test_a_source_is_required(self):
        with pytest.raises(SystemExit):
            fixlets.main([])

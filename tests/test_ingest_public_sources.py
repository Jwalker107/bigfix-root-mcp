"""The tracked public list and the private list: which index each row feeds."""

import shutil

import pytest

from tests.ingest_git import commit_files, make_repo
from tools.ingest import fixletbuild, fixlets, gitsource

COMMIT = "0123456789abcdef0123456789abcdef01234567"
HEADER = "id,repo_url,branch,commit,subpath,license,pinned_on,notes,content,group"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def public_csv(tmp_path, *rows, header=HEADER):
    path = tmp_path / "GIT_SOURCES.csv"
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def git_line(source_id, content="", group="", subpath="docs"):
    url = f"https://example.com/org/{source_id}.git"
    return f"{source_id},{url},main,{COMMIT},{subpath},MIT,2026-10-26,note,{content},{group}"


def local_csv(tmp_path, *rows, name="local-sources.csv"):
    path = tmp_path / name
    path.write_text(
        "id,kind,location,branch,commit,subpath,group,notes\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return path


class TestContentColumn:
    def test_a_row_that_says_nothing_feeds_the_documentation_index(self):
        assert gitsource.content_of({"content": ""}) == {"docs"}
        assert gitsource.content_of({}) == {"docs"}

    def test_each_kind_and_both_can_be_named(self):
        assert gitsource.content_of({"content": "fixlets"}) == {"fixlets"}
        assert gitsource.content_of({"content": "docs+fixlets"}) == {"docs", "fixlets"}
        assert gitsource.content_of({"content": " docs + fixlets "}) == {"docs", "fixlets"}

    def test_the_rows_are_split_between_the_two_indexes(self, tmp_path):
        rows = gitsource.read_git_sources(
            public_csv(
                tmp_path,
                git_line("a"),
                git_line("b", "fixlets"),
                git_line("c", "docs+fixlets"),
            )
        )
        assert [r["id"] for r in gitsource.docs_rows(rows)] == ["a", "c"]
        assert [r["id"] for r in gitsource.fixlet_rows(rows)] == ["b", "c"]

    def test_an_unknown_content_is_refused_with_a_reason(self, tmp_path):
        path = public_csv(tmp_path, git_line("a", "slides"))
        with pytest.raises(ValueError, match="content must be docs or fixlets"):
            gitsource.read_git_sources(path)

    def test_a_bad_group_is_refused(self, tmp_path):
        path = public_csv(tmp_path, git_line("a", "fixlets", "has space"))
        with pytest.raises(ValueError, match="invalid group"):
            gitsource.read_git_sources(path)

    def test_a_file_without_the_optional_columns_still_reads(self, tmp_path):
        path = public_csv(
            tmp_path,
            f"a,https://example.com/a.git,main,{COMMIT},docs,MIT,2026-10-26,n",
            header="id,repo_url,branch,commit,subpath,license,pinned_on,notes",
        )
        (row,) = gitsource.read_git_sources(path)
        assert row["content"] == ""
        assert gitsource.docs_rows([row]) == [row]

    def test_the_documentation_sync_leaves_a_fixlet_only_row_alone(self, tmp_path, capsys):
        path = public_csv(tmp_path, git_line("only-fixlets", "fixlets"))
        code = gitsource.main(["--sources", str(path), "--dry-run", "--raw-dir", str(tmp_path)])
        assert code == 0
        assert "no git sources to sync" in capsys.readouterr().out


class TestTheTrackedList:
    def test_the_two_public_repositories_are_listed_for_the_fixlet_index_only(self):
        rows = gitsource.read_git_sources(gitsource.GIT_SOURCES_PATH)
        by_id = {r["id"]: r for r in rows}
        for source_id in ("bigfix-community-content", "bigfix-content"):
            row = by_id[source_id]
            assert gitsource.content_of(row) == {"fixlets"}
            assert row["group"] == "reference"
            assert len(row["commit"]) == 40
        assert {"bigfix-community-content", "bigfix-content"}.isdisjoint(
            r["id"] for r in gitsource.docs_rows(rows)
        )

    def test_the_documentation_sources_are_unchanged(self):
        rows = gitsource.read_git_sources(gitsource.GIT_SOURCES_PATH)
        ids = [r["id"] for r in gitsource.docs_rows(rows)]
        assert "developer-bigfix-com" in ids
        assert "developer-bigfix-com-action-script" in ids


class TestPublicSources:
    def test_a_fixlet_row_becomes_a_pinned_git_source(self, tmp_path):
        path = public_csv(tmp_path, git_line("pub", "fixlets", "reference", subpath="."))
        (row,) = fixlets.public_sources(path)
        assert row["id"] == "pub"
        assert row["kind"] == "git"
        assert row["location"] == "https://example.com/org/pub.git"
        assert row["commit"] == COMMIT
        assert row["group"] == "reference"
        assert row["subpath"] == "."

    def test_a_documentation_row_is_not_a_fixlet_source(self, tmp_path):
        assert fixlets.public_sources(public_csv(tmp_path, git_line("docs-only"))) == []

    def test_a_missing_list_gives_nothing(self, tmp_path):
        assert fixlets.public_sources(tmp_path / "absent.csv") == []


class TestAllSources:
    def test_the_private_list_comes_first_then_the_public_rows(self, tmp_path):
        local = local_csv(tmp_path, "mine,dir,C:/x,,,,team,")
        public = public_csv(tmp_path, git_line("pub", "fixlets", "reference"))
        assert [r["id"] for r in fixlets.all_sources(local, public)] == ["mine", "pub"]

    def test_the_same_id_in_both_lists_is_refused(self, tmp_path):
        local = local_csv(tmp_path, "pub,dir,C:/x,,,,team,")
        public = public_csv(tmp_path, git_line("pub", "fixlets"))
        with pytest.raises(ValueError, match="used in both lists: pub"):
            fixlets.all_sources(local, public)

    def test_no_private_list_is_fine_when_there_are_public_rows(self, tmp_path):
        public = public_csv(tmp_path, git_line("pub", "fixlets"))
        rows = fixlets.all_sources(tmp_path / "local-sources.csv", public)
        assert [r["id"] for r in rows] == ["pub"]

    def test_the_old_file_name_gets_a_rename_hint(self, tmp_path):
        local_csv(tmp_path, "mine,dir,C:/x,,,,team,", name="fixlet-sources.csv")
        with pytest.raises(
            ValueError, match=r"fixlet-sources\.csv is now called local-sources\.csv"
        ):
            fixlets.all_sources(tmp_path / "local-sources.csv", tmp_path / "none.csv")

    def test_the_private_list_has_its_new_name(self):
        assert fixlets.SOURCES_PATH.name == "local-sources.csv"


class TestCommandLines:
    def args(self, tmp_path, local, public, *more):
        return fixletbuild._parse_args(
            ["--sources", str(local), "--public-sources", str(public), *more]
        )

    def test_all_reads_both_lists(self, tmp_path):
        local = local_csv(tmp_path, "mine,dir,C:/x,,,,team,")
        public = public_csv(tmp_path, git_line("pub", "fixlets"))
        rows = fixletbuild._requested_rows(self.args(tmp_path, local, public, "--all"))
        assert [r["id"] for r in rows] == ["mine", "pub"]

    def test_a_public_source_can_be_named(self, tmp_path):
        local = local_csv(tmp_path, "mine,dir,C:/x,,,,team,")
        public = public_csv(tmp_path, git_line("pub", "fixlets"))
        rows = fixletbuild._requested_rows(self.args(tmp_path, local, public, "--source", "pub"))
        assert [r["id"] for r in rows] == ["pub"]

    def test_an_unknown_source_says_so(self, tmp_path):
        local = local_csv(tmp_path, "mine,dir,C:/x,,,,team,")
        public = public_csv(tmp_path, git_line("pub", "fixlets"))
        with pytest.raises(ValueError, match="no such source: nope"):
            fixletbuild._requested_rows(self.args(tmp_path, local, public, "--source", "nope"))

    def test_all_with_no_sources_at_all_is_an_error_not_an_empty_build(self, tmp_path):
        local = local_csv(tmp_path)
        public = public_csv(tmp_path, git_line("docs-only"))
        with pytest.raises(ValueError, match="no sources to read"):
            fixletbuild._requested_rows(self.args(tmp_path, local, public, "--all"))


@needs_git
class TestReadingAPublicRow:
    def test_a_pinned_public_repository_is_read_for_its_bes_files(self, tmp_path, capsys):
        repo = make_repo(tmp_path / "upstream")
        bes = (
            '<?xml version="1.0" encoding="UTF-8"?><BES><Fixlet><Title>Public one</Title>'
            "<Relevance>windows of operating system</Relevance></Fixlet></BES>"
        )
        commit = commit_files(repo, {"content/one.bes": bes, "README.md": "# hi\n"})
        public = tmp_path / "GIT_SOURCES.csv"
        public.write_text(
            HEADER
            + "\n"
            + f"pub,{repo.as_uri()},,{commit},content,MIT,2026-10-26,n,fixlets,reference\n",
            encoding="utf-8",
        )
        code = fixlets.main(
            [
                "--source",
                "pub",
                "--sources",
                str(tmp_path / "none.csv"),
                "--public-sources",
                str(public),
                "--allow-scheme",
                "file",
                "--raw-dir",
                str(tmp_path / "raw"),
                "--reads-log",
                str(tmp_path / "reads.jsonl"),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert commit[:12] in out
        assert "items with relevance: 1" in out


class TestPrivateDocuments:
    """A `file` row of the private list feeds the documentation index, not this one."""

    def test_a_file_row_is_kept_out_of_the_fixlet_sources(self, tmp_path):
        local = local_csv(
            tmp_path,
            "team,dir,C:/fixlets,,,,team,",
            "guide,file,C:/docs/guide.pdf,,,,team,private",
        )
        listed = fixlets.all_sources(local, tmp_path / "none.csv")
        assert [row["id"] for row in listed] == ["team"]
        assert [row["id"] for row in fixlets.document_sources(local)] == ["guide"]

    def test_a_file_row_takes_no_branch_commit_or_subpath(self, tmp_path):
        local = local_csv(tmp_path, "guide,file,C:/docs/guide.pdf,main,,,team,")
        with pytest.raises(ValueError, match="takes no branch"):
            fixlets.read_sources(local)

    def test_no_private_list_means_no_documents(self, tmp_path):
        assert fixlets.document_sources(tmp_path / "missing.csv") == []

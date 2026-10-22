"""tools/ingest/gitsource.py: pinned git checkouts, tested against local repositories."""

import shutil

import pytest

from tests.ingest_git import commit_files, git, git_row, make_repo
from tools.ingest import fetch, gitsource

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

FILE = ("file",)  # tests fetch from local file:// repositories


@pytest.fixture
def repo(tmp_path):
    path = make_repo(tmp_path / "upstream")
    commit = commit_files(path, {"pages/a.md": "# A\n\nText.\n"}, "first")
    return path, commit


def sync(row, raw_dir, **kwargs):
    return gitsource.sync_git_source(row, raw_dir, allow_schemes=FILE, **kwargs)


class TestSync:
    def test_a_missing_checkout_is_cloned_at_the_pinned_commit(self, repo, tmp_path):
        path, commit = repo
        raw = tmp_path / "raw"
        result = sync(git_row(path, commit), raw)
        assert result.status == fetch.STATUS_NEW
        checkout = raw / "docs"
        assert git(checkout, "rev-parse", "HEAD") == commit
        assert (checkout / "pages" / "a.md").read_text(encoding="utf-8") == "# A\n\nText.\n"

    def test_a_second_run_changes_nothing(self, repo, tmp_path):
        path, commit = repo
        raw = tmp_path / "raw"
        sync(git_row(path, commit), raw)
        assert sync(git_row(path, commit), raw).status == fetch.STATUS_UNCHANGED

    def test_a_newer_pin_moves_the_checkout(self, repo, tmp_path):
        path, commit = repo
        raw = tmp_path / "raw"
        sync(git_row(path, commit), raw)
        newer = commit_files(path, {"pages/a.md": "# A\n\nChanged.\n"}, "second")
        result = sync(git_row(path, newer), raw)
        assert result.status == fetch.STATUS_CHANGED
        assert git(raw / "docs", "rev-parse", "HEAD") == newer
        assert "Changed." in (raw / "docs" / "pages" / "a.md").read_text(encoding="utf-8")

    def test_an_older_pin_is_honoured_even_when_upstream_has_moved_on(self, repo, tmp_path):
        path, commit = repo
        commit_files(path, {"pages/a.md": "# A\n\nLater.\n"}, "second")
        raw = tmp_path / "raw"
        sync(git_row(path, commit), raw)
        assert git(raw / "docs", "rev-parse", "HEAD") == commit

    def test_a_modified_checkout_is_never_overwritten(self, repo, tmp_path):
        path, commit = repo
        raw = tmp_path / "raw"
        sync(git_row(path, commit), raw)
        edited = raw / "docs" / "pages" / "a.md"
        edited.write_text("local edit", encoding="utf-8")
        newer = commit_files(path, {"pages/b.md": "# B\n"}, "second")
        result = sync(git_row(path, newer), raw)
        assert result.status == fetch.STATUS_ERROR
        assert "local modifications" in result.detail
        assert edited.read_text(encoding="utf-8") == "local edit"

    def test_an_untracked_file_also_counts_as_a_modification(self, repo, tmp_path):
        path, commit = repo
        raw = tmp_path / "raw"
        sync(git_row(path, commit), raw)
        (raw / "docs" / "stray.txt").write_text("x", encoding="utf-8")
        assert sync(git_row(path, commit), raw).status == fetch.STATUS_ERROR

    def test_converted_line_endings_count_as_modified_even_when_status_says_clean(
        self, repo, tmp_path
    ):
        path, commit = repo
        raw = tmp_path / "raw"
        sync(git_row(path, commit), raw)
        checkout = raw / "docs"
        assert gitsource.is_clean(checkout, allow_schemes=FILE)
        target = checkout / "pages" / "a.md"
        target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
        # a status run under autocrlf=true refreshes git's stat cache and sees no change
        git(checkout, "-c", "core.autocrlf=true", "status", "--porcelain")
        assert not gitsource.is_clean(checkout, allow_schemes=FILE)
        result = sync(git_row(path, commit), raw)
        assert result.status == fetch.STATUS_ERROR
        assert "local modifications" in result.detail

    def test_a_checkout_of_a_different_origin_is_refused(self, repo, tmp_path):
        path, commit = repo
        other = make_repo(tmp_path / "other")
        other_commit = commit_files(other, {"pages/a.md": "x"}, "o")
        raw = tmp_path / "raw"
        sync(git_row(path, commit), raw)
        result = sync(git_row(other, other_commit), raw)
        assert result.status == fetch.STATUS_ERROR
        assert "origin" in result.detail

    def test_a_scheme_that_is_not_allowed_is_refused(self, repo, tmp_path):
        path, commit = repo
        result = gitsource.sync_git_source(git_row(path, commit), tmp_path / "raw")
        assert result.status == fetch.STATUS_ERROR
        assert "not allowed" in result.detail
        assert not (tmp_path / "raw").exists()

    def test_dry_run_reports_without_touching_disk(self, repo, tmp_path):
        path, commit = repo
        raw = tmp_path / "raw"
        result = sync(git_row(path, commit), raw, dry_run=True)
        assert result.status == fetch.STATUS_NEW
        assert "would be" in result.detail
        assert not raw.exists()

    def test_dry_run_reports_a_pending_move(self, repo, tmp_path):
        path, commit = repo
        raw = tmp_path / "raw"
        sync(git_row(path, commit), raw)
        newer = commit_files(path, {"pages/b.md": "# B\n"}, "second")
        result = sync(git_row(path, newer), raw, dry_run=True)
        assert result.status == fetch.STATUS_CHANGED
        assert git(raw / "docs", "rev-parse", "HEAD") == commit

    def test_an_unknown_commit_fails_and_leaves_no_half_made_checkout(self, repo, tmp_path):
        path, _ = repo
        raw = tmp_path / "raw"
        result = sync(git_row(path, "0" * 40), raw)
        assert result.status == fetch.STATUS_ERROR
        assert not (raw / "docs").exists()

    def test_a_non_git_directory_in_the_way_is_not_touched(self, repo, tmp_path):
        path, commit = repo
        raw = tmp_path / "raw"
        (raw / "docs").mkdir(parents=True)
        (raw / "docs" / "mine.txt").write_text("keep", encoding="utf-8")
        result = sync(git_row(path, commit), raw)
        assert result.status == fetch.STATUS_ERROR
        assert (raw / "docs" / "mine.txt").read_text(encoding="utf-8") == "keep"


class TestReadGitSources:
    HEADER = ",".join(gitsource.GIT_COLUMNS)
    SHA = "a" * 40

    def read(self, tmp_path, row):
        path = tmp_path / "GIT_SOURCES.csv"
        path.write_text(f"{self.HEADER}\n{row}\n", encoding="utf-8")
        return gitsource.read_git_sources(path)

    def good(self, **overrides):
        fields = {
            "id": "docs",
            "repo_url": "https://example.com/r.git",
            "branch": "master",
            "commit": self.SHA,
            "subpath": "site/pages",
            "license": "",
            "pinned_on": "",
            "notes": "",
        }
        fields.update(overrides)
        return ",".join(fields[name] for name in gitsource.GIT_COLUMNS)

    def test_a_valid_row_is_read(self, tmp_path):
        (row,) = self.read(tmp_path, self.good())
        assert (row["id"], row["commit"], row["subpath"]) == ("docs", self.SHA, "site/pages")

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"id": "../x"}, "invalid id"),
            ({"commit": "abc123"}, "40-character"),
            ({"commit": "A" * 40}, "40-character"),
            ({"repo_url": "--upload-pack=evil"}, "invalid repo_url"),
            ({"repo_url": "https://a b/r.git"}, "invalid repo_url"),
            ({"subpath": "../escape"}, "subpath"),
            ({"subpath": "a/../../b"}, "subpath"),
            ({"subpath": "/abs"}, "subpath"),
            ({"subpath": "C:/win"}, "subpath"),
            ({"subpath": ""}, "subpath"),
        ],
    )
    def test_unsafe_fields_are_rejected(self, tmp_path, overrides, message):
        with pytest.raises(ValueError, match=message):
            self.read(tmp_path, self.good(**overrides))

    def test_duplicate_ids_are_rejected(self, tmp_path):
        path = tmp_path / "GIT_SOURCES.csv"
        path.write_text(f"{self.HEADER}\n{self.good()}\n{self.good()}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate"):
            gitsource.read_git_sources(path)

    def test_the_shipped_file_is_valid(self):
        rows = gitsource.read_git_sources(gitsource.GIT_SOURCES_PATH)
        assert rows
        assert all(len(row["commit"]) == 40 for row in rows)


class TestMain:
    def test_the_command_line_only_accepts_https_urls(self, repo, tmp_path, capsys):
        path, commit = repo
        sources = tmp_path / "GIT_SOURCES.csv"
        row = git_row(path, commit)  # a file:// URL
        sources.write_text(
            ",".join(gitsource.GIT_COLUMNS)
            + "\n"
            + ",".join(row[name] for name in gitsource.GIT_COLUMNS)
            + "\n",
            encoding="utf-8",
        )
        assert gitsource.main(["--sources", str(sources), "--raw-dir", str(tmp_path / "raw")]) == 1
        assert "not allowed" in capsys.readouterr().out
        assert not (tmp_path / "raw").exists()

    def test_an_unreadable_sources_file_exits_2(self, tmp_path):
        assert gitsource.main(["--sources", str(tmp_path / "missing.csv")]) == 2

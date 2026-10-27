"""Throwaway local git repositories for the ingest tests (no network)."""

import subprocess
from pathlib import Path

_IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *_IDENTITY, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def make_repo(path: Path) -> Path:
    """An empty repository that lets a client fetch any commit by SHA."""
    path.mkdir(parents=True)
    git(path, "init", "--quiet")
    git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    git(path, "config", "core.autocrlf", "false")
    return path


def commit_files(repo: Path, files: dict[str, str], message: str = "commit") -> str:
    """Write files (relative path -> text), commit them, and return the commit SHA."""
    for relative, text in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "--message", message)
    return git(repo, "rev-parse", "HEAD")


def git_row(repo: Path, commit: str, *, source_id: str = "docs", subpath: str = "pages") -> dict:
    return {
        "id": source_id,
        "repo_url": repo.as_uri(),
        "branch": "master",
        "commit": commit,
        "subpath": subpath,
        "license": "Apache-2.0",
        "pinned_on": "2026-10-21",
        "notes": "",
    }

"""Check out the git sources pinned in corpus/GIT_SOURCES.csv.

Each row pins one repository at one commit. This tool makes the untracked
checkout under <raw>/<id>/ match that commit exactly, so an index can be rebuilt
from the same bytes on a fresh machine.

Safety:
- Every manifest field is validated before it reaches a command line: the id
  and commit are matched against strict patterns, the URL cannot start with a
  dash, and the subpath cannot leave the repository.
- git runs with protocol.allow=never and only the URL's own scheme re-enabled
  (https by default), so an `ext::` or `file:` URL in the manifest cannot run
  anything. Prompts for credentials are disabled; a private repository fails
  rather than asking.
- A checkout with local modifications is never overwritten or moved.
- After every fetch the resulting HEAD is compared with the pinned commit.

Stdout carries the report; diagnostics go to stderr via logging.
"""

import argparse
import csv
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Callable, Collection, Sequence
from pathlib import Path

from tools.ingest import fetch

log = logging.getLogger("corpus.gitsource")

GIT_SOURCES_PATH = fetch.REPO_CORPUS / "GIT_SOURCES.csv"
GIT_COLUMNS = ("id", "repo_url", "branch", "commit", "subpath", "license", "pinned_on", "notes")
REQUIRED_GIT_COLUMNS = ("id", "repo_url", "commit", "subpath")

DEFAULT_ALLOWED_SCHEMES = ("https",)
GIT_TIMEOUT_SECONDS = 600

# `git ls-files --eol` prints: i/<index eol> w/<worktree eol> attr/<attributes...> <TAB> path
_EOL_FIELDS = 3

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
# a branch name that cannot start with a dash or hold characters git treats specially
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")

Runner = Callable[..., subprocess.CompletedProcess]


def read_git_sources(path: Path) -> list[dict[str, str]]:
    """Read and validate the git sources file. Raises ValueError if malformed."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        missing = [name for name in REQUIRED_GIT_COLUMNS if name not in fieldnames]
        if missing:
            raise ValueError(f"{path}: missing required column(s): {', '.join(missing)}")
        rows = [{name: (row.get(name) or "").strip() for name in fieldnames} for row in reader]
    seen: set[str] = set()
    for row in rows:
        _validate_row(path, row)
        if row["id"] in seen:
            raise ValueError(f"{path}: duplicate id {row['id']!r}")
        seen.add(row["id"])
    return rows


def _validate_row(path: Path, row: dict[str, str]) -> None:
    source_id = row["id"]
    if not _ID_RE.match(source_id):
        raise ValueError(f"{path}: invalid id {source_id!r} (use letters, digits, . _ -)")
    if not _COMMIT_RE.match(row["commit"]):
        raise ValueError(f"{path}: {source_id}: commit must be a full 40-character lowercase SHA")
    url = row["repo_url"]
    if not url or url.startswith("-") or any(ch.isspace() for ch in url):
        raise ValueError(f"{path}: {source_id}: invalid repo_url {url!r}")
    subpath = row["subpath"]
    parts = subpath.replace("\\", "/").split("/")
    if (
        not subpath
        or "\\" in subpath
        or subpath.startswith("/")
        or re.match(r"^[A-Za-z]:", subpath)
        or ".." in parts
    ):
        raise ValueError(f"{path}: {source_id}: subpath must be a relative path inside the repo")


def checkout_dir(raw_dir: Path, source_id: str) -> Path:
    """The checkout directory for a source id (the id is validated by read_git_sources)."""
    return raw_dir / source_id


def _run_git(
    args: Sequence[str],
    cwd: Path,
    *,
    allow_schemes: Collection[str],
    runner: Runner,
) -> subprocess.CompletedProcess:
    command = ["git", "-c", "protocol.allow=never"]
    for scheme in sorted(allow_schemes):
        command += ["-c", f"protocol.{scheme}.allow=always"]
    # keep checkout bytes identical to the repository's, whatever the user's config says
    command += ["-c", "core.autocrlf=false", *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return runner(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        env=env,
        check=False,
    )


def _git_out(args: Sequence[str], cwd: Path, **kwargs) -> str:
    """Run git, returning stripped stdout. Raises RuntimeError with git's message on failure."""
    done = _run_git(args, cwd, **kwargs)
    if done.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def head_commit(
    checkout: Path,
    *,
    allow_schemes: Collection[str] = DEFAULT_ALLOWED_SCHEMES,
    runner: Runner = subprocess.run,
) -> str:
    """HEAD of a checkout. Raises RuntimeError if it is not a usable git checkout."""
    return _git_out(["rev-parse", "HEAD"], checkout, allow_schemes=allow_schemes, runner=runner)


def commit_date(
    checkout: Path,
    *,
    allow_schemes: Collection[str] = DEFAULT_ALLOWED_SCHEMES,
    runner: Runner = subprocess.run,
) -> str:
    """The committer date of HEAD as a strict ISO 8601 string (git's own record)."""
    return _git_out(
        ["show", "--no-patch", "--format=%cI", "HEAD"],
        checkout,
        allow_schemes=allow_schemes,
        runner=runner,
    )


def resolve_tip(
    repo_url: str,
    branch: str,
    *,
    allow_schemes: Collection[str] = DEFAULT_ALLOWED_SCHEMES,
    runner: Runner = subprocess.run,
) -> str:
    """The commit a branch (or, if blank, the remote's HEAD) points to right now.

    Read-only: it asks the remote and fetches nothing. The url and branch are checked
    before they reach a command line. Raises ValueError for a value that is not
    acceptable, RuntimeError if the remote cannot be asked or has no such branch.
    """
    if not repo_url or repo_url.startswith("-") or any(ch.isspace() for ch in repo_url):
        raise ValueError(f"invalid repo_url {repo_url!r}")
    if branch and not _BRANCH_RE.match(branch):
        raise ValueError(f"invalid branch {branch!r}")
    ref = f"refs/heads/{branch}" if branch else "HEAD"
    with tempfile.TemporaryDirectory() as scratch:
        out = _git_out(
            ["ls-remote", "--exit-code", repo_url, ref],
            Path(scratch),
            allow_schemes=allow_schemes,
            runner=runner,
        )
    sha = out.split("\t", 1)[0].strip()
    if not _COMMIT_RE.match(sha):
        raise RuntimeError(f"could not resolve {ref!r} at the remote")
    return sha


def is_clean(
    checkout: Path,
    *,
    allow_schemes: Collection[str] = DEFAULT_ALLOWED_SCHEMES,
    runner: Runner = subprocess.run,
) -> bool:
    """True when the checkout matches its commit byte for byte.

    That means no modified, staged or untracked files, and no file whose line
    endings were converted (for example by a user's core.autocrlf setting). The
    second check reads the files themselves: git's status can report such a
    checkout as clean when its stat cache was refreshed under a different
    autocrlf setting.
    """
    status = _git_out(
        ["status", "--porcelain", "--untracked-files=all"],
        checkout,
        allow_schemes=allow_schemes,
        runner=runner,
    )
    if status:
        return False
    listing = _git_out(["ls-files", "--eol"], checkout, allow_schemes=allow_schemes, runner=runner)
    for line in listing.splitlines():
        meta = line.split("\t", 1)[0].split()
        if len(meta) < _EOL_FIELDS:
            continue
        in_index, in_worktree = meta[0].removeprefix("i/"), meta[1].removeprefix("w/")
        # a file with an explicit eol attribute is meant to differ from the index
        if in_index != in_worktree and not any(field.startswith("eol=") for field in meta[2:]):
            return False
    return True


def _force_remove(func, path, _exc_info) -> None:
    """shutil.rmtree error handler: git marks object files read-only on Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _fetch_and_checkout(
    row: dict[str, str],
    dest: Path,
    *,
    allow_schemes: Collection[str],
    runner: Runner,
) -> None:
    kwargs = {"allow_schemes": allow_schemes, "runner": runner}
    _git_out(["fetch", "--quiet", "--depth", "1", "origin", row["commit"]], dest, **kwargs)
    _git_out(["checkout", "--quiet", "--detach", "FETCH_HEAD"], dest, **kwargs)
    head = _git_out(["rev-parse", "HEAD"], dest, **kwargs)
    if head != row["commit"]:
        raise RuntimeError(f"checked out {head}, expected the pinned {row['commit']}")


def sync_git_source(
    row: dict[str, str],
    raw_dir: Path,
    *,
    dry_run: bool = False,
    allow_schemes: Collection[str] = DEFAULT_ALLOWED_SCHEMES,
    runner: Runner = subprocess.run,
) -> fetch.Result:
    """Bring one source's checkout to its pinned commit.

    Statuses: new (cloned), changed (moved to the pinned commit), unchanged,
    error. With dry_run nothing is fetched or written; new/changed then mean
    "would be".
    """
    source_id = row["id"]
    scheme = urllib.parse.urlsplit(row["repo_url"]).scheme
    if scheme not in allow_schemes:
        return fetch.Result(
            source_id,
            fetch.STATUS_ERROR,
            f"URL scheme {scheme!r} is not allowed (allowed: {', '.join(sorted(allow_schemes))})",
        )
    dest = checkout_dir(raw_dir, source_id)
    kwargs = {"allow_schemes": allow_schemes, "runner": runner}
    verb = "would be " if dry_run else ""
    try:
        if (dest / ".git").is_dir():
            return _sync_existing(row, dest, dry_run=dry_run, verb=verb, **kwargs)
        if dest.exists() and any(dest.iterdir()):
            return fetch.Result(
                source_id, fetch.STATUS_ERROR, f"{dest} exists but is not a git checkout"
            )
        if dry_run:
            return fetch.Result(
                source_id, fetch.STATUS_NEW, f"{verb}cloned at {row['commit'][:12]}"
            )
        created = not dest.exists()
        dest.mkdir(parents=True, exist_ok=True)
        try:
            _git_out(["init", "--quiet"], dest, **kwargs)
            _git_out(["remote", "add", "origin", row["repo_url"]], dest, **kwargs)
            _fetch_and_checkout(row, dest, **kwargs)
        except BaseException:
            if created:  # never leave a half-made checkout that looks real
                shutil.rmtree(dest, onerror=_force_remove)
            raise
        return fetch.Result(source_id, fetch.STATUS_NEW, f"cloned at {row['commit'][:12]}")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as err:
        return fetch.Result(source_id, fetch.STATUS_ERROR, str(err))


def _sync_existing(
    row: dict[str, str],
    dest: Path,
    *,
    dry_run: bool,
    verb: str,
    allow_schemes: Collection[str],
    runner: Runner,
) -> fetch.Result:
    source_id = row["id"]
    kwargs = {"allow_schemes": allow_schemes, "runner": runner}
    origin = _git_out(["remote", "get-url", "origin"], dest, **kwargs)
    if origin != row["repo_url"]:
        return fetch.Result(
            source_id,
            fetch.STATUS_ERROR,
            f"checkout's origin is {origin!r}, manifest says {row['repo_url']!r}",
        )
    if not is_clean(dest, **kwargs):
        return fetch.Result(
            source_id,
            fetch.STATUS_ERROR,
            "checkout has local modifications; refusing to touch it (remove it to re-clone)",
        )
    head = head_commit(dest, **kwargs)
    if head == row["commit"]:
        return fetch.Result(source_id, fetch.STATUS_UNCHANGED, f"at {head[:12]}")
    if dry_run:
        return fetch.Result(
            source_id, fetch.STATUS_CHANGED, f"{verb}moved {head[:12]} -> {row['commit'][:12]}"
        )
    _fetch_and_checkout(row, dest, **kwargs)
    return fetch.Result(
        source_id, fetch.STATUS_CHANGED, f"moved {head[:12]} -> {row['commit'][:12]}"
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.ingest.gitsource",
        description="Check out the git sources in corpus/GIT_SOURCES.csv at their pinned commits.",
    )
    parser.add_argument("--dry-run", action="store_true", help="report only; fetch nothing")
    parser.add_argument("--only", action="append", metavar="ID", help="limit to a source id")
    parser.add_argument("--sources", type=Path, default=GIT_SOURCES_PATH)
    parser.add_argument(
        "--raw-dir", type=Path, help="default: $BIGFIX_CORPUS_DIR/raw or corpus/raw"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Exit 0 ok, 1 if any source errored, 2 if the sources file is unusable."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        rows = read_git_sources(args.sources)
    except (OSError, ValueError) as err:
        # a bad sources file is a user error; a traceback would only bury the message
        log.error("cannot read git sources: %s", err)  # noqa: TRY400
        return 2
    raw_dir = args.raw_dir or fetch.default_raw_dir()
    results = [
        sync_git_source(row, raw_dir, dry_run=args.dry_run)
        for row in rows
        if not args.only or row["id"] in args.only
    ]
    for result in results:
        sys.stdout.write(f"{result.status:<10} {result.source_id}  {result.detail}".rstrip() + "\n")
    if not results:
        sys.stdout.write("no git sources to sync\n")
    return 1 if any(result.status == fetch.STATUS_ERROR for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())

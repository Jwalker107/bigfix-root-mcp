"""Read reference fixlets from a local directory or a git repository.

A source is either

  dir  a local directory, read in place and never modified; or
  git  a repository, checked out under the untracked raw directory at one commit.

Every read leaves a record saying what was read and when: the UTC time, the commit
(git) and a fingerprint of the files (both), with counts. The records are appended
to an untracked log, so "when was this source last read, and was it the same
content" can be answered later. A dry run changes nothing and writes no record.

Action scripts are read as well (the index keeps a short excerpt after a secret scan);
they are never printed in a report.

Confidentiality: this is company content. Nothing here is written to the repository;
the source list, the checkouts and the read log are all untracked. A source's own
location is internal, so it stays in the untracked list as well.

Safety:
- the source list is validated before any value reaches a command line or the
  filesystem; a git URL may not carry a password, and a user name is allowed only for
  ssh, where it is the login and not a secret;
- git is driven through gitsource.py: allowed URL schemes only, no credential
  prompts, protocols locked, and a check that HEAD is the commit asked for;
- the directory walk never follows a symbolic link or junction (see bes.py);
- a subpath must stay inside its source after links are resolved.

    python -m tools.ingest.fixlets --source ID
    python -m tools.ingest.fixlets --dir PATH
    python -m tools.ingest.fixlets --git URL [--branch B] [--commit SHA] [--subpath P]

Stdout carries the report; diagnostics go to stderr via logging.
"""

import argparse
import csv
import hashlib
import json
import logging
import re
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from tools.ingest import bes, fetch, gitsource

log = logging.getLogger("corpus.fixlets")

# the untracked list of private sources: a company directory or an internal repository
SOURCES_PATH = fetch.REPO_CORPUS / "local-sources.csv"
LEGACY_SOURCES_NAME = "fixlet-sources.csv"  # its name before it was renamed
# the tracked list of public sources; the rows that say `content=fixlets` feed this index
PUBLIC_SOURCES_PATH = gitsource.GIT_SOURCES_PATH
COLUMNS = ("id", "kind", "location", "branch", "commit", "subpath", "group", "notes")
REQUIRED_COLUMNS = ("id", "kind", "location")
KINDS = ("dir", "git", "file")
# a `file` row is one private document (PDF, HTML, markdown, text) for the documentation
# index (tools/ingest/build.py); only `dir` and `git` rows hold Fixlets
FIXLET_KINDS = ("dir", "git")
DOCUMENT_KIND = "file"
READS_NAME = "fixlet-reads.jsonl"
RAW_SUBDIR = "fixlets"  # checkouts live apart from the documentation sources

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SSH = "ssh"

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_row(row: dict[str, str], label: str = "source") -> None:
    """Raise ValueError if a source row could not be used safely."""
    source_id = row.get("id", "")
    if not _ID_RE.match(source_id):
        raise ValueError(f"{label}: invalid id {source_id!r} (use letters, digits, . _ -)")
    kind = row.get("kind", "")
    if kind not in KINDS:
        raise ValueError(f"{label} {source_id}: kind must be one of {', '.join(KINDS)}")
    location = row.get("location", "")
    if not location:
        raise ValueError(f"{label} {source_id}: location is empty")
    group = row.get("group", "")
    if group and not _ID_RE.match(group):
        raise ValueError(
            f"{label} {source_id}: invalid group {group!r} (use letters, digits, . _ -)"
        )
    subpath = row.get("subpath", "")
    parts = subpath.replace("\\", "/").split("/")
    if subpath and (
        "\\" in subpath
        or subpath.startswith("/")
        or re.match(r"^[A-Za-z]:", subpath)
        or ".." in parts
    ):
        raise ValueError(f"{label} {source_id}: subpath must be a relative path inside the source")
    if kind == DOCUMENT_KIND and (subpath or row.get("branch") or row.get("commit")):
        raise ValueError(f"{label} {source_id}: a file source takes no branch, commit or subpath")
    if kind in ("dir", DOCUMENT_KIND):
        return
    if location.startswith("-") or any(ch.isspace() for ch in location):
        raise ValueError(f"{label} {source_id}: invalid git location")
    parsed = urllib.parse.urlsplit(location)
    if parsed.password is not None:
        raise ValueError(f"{label} {source_id}: a password in the URL is not accepted")
    if parsed.username is not None and parsed.scheme != _SSH:
        raise ValueError(f"{label} {source_id}: credentials in the URL are not accepted")
    commit = row.get("commit", "")
    if commit and not _COMMIT_RE.match(commit):
        raise ValueError(f"{label} {source_id}: commit must be blank or a full 40-character SHA")


def read_sources(path: Path) -> list[dict[str, str]]:
    """Read and validate the source list. Raises ValueError if malformed."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
        if missing:
            raise ValueError(f"{path}: missing required column(s): {', '.join(missing)}")
        rows = [{name: (row.get(name) or "").strip() for name in COLUMNS} for row in reader]
    seen: set[str] = set()
    for row in rows:
        validate_row(row, str(path))
        if row["id"] in seen:
            raise ValueError(f"{path}: duplicate id {row['id']!r}")
        seen.add(row["id"])
    return rows


def public_sources(path: Path = PUBLIC_SOURCES_PATH) -> list[dict[str, str]]:
    """The public git sources that feed this index, from the tracked git sources list.

    A row feeds it when its `content` says `fixlets` (or `docs+fixlets`). Each is already
    pinned to a commit and a subpath by that list, so a read of it is reproducible.
    """
    if not path.exists():
        return []
    rows = []
    for row in gitsource.fixlet_rows(gitsource.read_git_sources(path)):
        converted = {
            "id": row["id"],
            "kind": "git",
            "location": row["repo_url"],
            "branch": row["branch"],
            "commit": row["commit"],
            "subpath": row["subpath"],
            "group": row["group"],
            "notes": row["notes"],
        }
        validate_row(converted, str(path))
        rows.append(converted)
    return rows


def all_sources(
    local_path: Path = SOURCES_PATH, public_path: Path = PUBLIC_SOURCES_PATH
) -> list[dict[str, str]]:
    """Every source: the private list first, then the public ones. Raises ValueError if unusable."""
    legacy = local_path.with_name(LEGACY_SOURCES_NAME)
    if not local_path.exists() and legacy.exists():
        raise ValueError(f"{legacy.name} is now called {local_path.name}: rename the file")
    local = read_sources(local_path) if local_path.exists() else []
    local = [row for row in local if row["kind"] in FIXLET_KINDS]
    public = public_sources(public_path)
    ids = [row["id"] for row in [*local, *public]]
    clash = sorted({i for i in ids if ids.count(i) > 1})
    if clash:
        raise ValueError(f"source id used in both lists: {', '.join(clash)}")
    return [*local, *public]


def document_sources(local_path: Path = SOURCES_PATH) -> list[dict[str, str]]:
    """The private documents (`file` rows) of the private list, for the documentation index."""
    if not local_path.exists():
        return []
    return [row for row in read_sources(local_path) if row["kind"] == DOCUMENT_KIND]


def adhoc_row(
    *,
    directory: str | None = None,
    repo: str | None = None,
    branch: str = "",
    commit: str = "",
    subpath: str = "",
    group: str = "",
) -> dict[str, str]:
    """A source row for a one-off command line, with an id derived from its location."""
    location = directory if directory is not None else repo or ""
    kind = "dir" if directory is not None else "git"
    if kind == "dir":
        # the same directory always gets the same id, however it was spelled
        location = str(Path(location).expanduser().resolve())
    digest = hashlib.sha256(location.encode("utf-8")).hexdigest()[:8]
    return {
        "id": f"{kind}-{digest}",
        "kind": kind,
        "location": location,
        "branch": branch,
        "commit": commit,
        "subpath": subpath,
        "group": group,
        "notes": "",
    }


@dataclass
class Read:
    """The outcome of reading one source."""

    source_id: str
    error: str = ""
    record: dict = field(default_factory=dict)
    extraction: bes.Extraction | None = None
    note: str = ""


def _inside(base: Path, subpath: str) -> Path:
    """base/subpath with links resolved, which must still be inside base."""
    resolved_base = base.resolve()
    target = (resolved_base / subpath).resolve() if subpath else resolved_base
    try:
        target.relative_to(resolved_base)
    except ValueError as err:
        raise ValueError("subpath leaves the source") from err
    if not target.is_dir():
        raise ValueError("no such directory in the source")
    return target


def _record(row: dict[str, str], when: str, extraction: bes.Extraction, **extra: str) -> dict:
    return {
        "source_id": row["id"],
        "kind": row["kind"],
        "location": row["location"],
        "subpath": row["subpath"],
        # sources in one group are counted together; a source without one stands alone
        "group": row.get("group") or row["id"],
        "read_at": when,
        "tree_sha256": extraction.fingerprint,
        "files": extraction.files,
        "duplicate_files": extraction.duplicate_files,
        "unparsable_files": len(extraction.unparsable),
        "items": len(extraction.items),
        **extra,
    }


def _read_directory(row: dict[str, str], when: str) -> Read:
    directory = Path(row["location"]).expanduser()
    # a `dir` row may also name one .bes file, so a single item can be added without
    # reading everything else in its folder
    single = directory.is_file() and directory.suffix.lower() == bes.EXTENSION
    if not (directory.is_dir() or single):
        return Read(row["id"], error=f"{directory} is not a directory or a .bes file")
    if single and row["subpath"]:
        return Read(row["id"], error="a single .bes file takes no subpath")
    resolved = {**row, "location": str(directory.resolve())}
    try:
        root = directory.resolve() if single else _inside(directory, row["subpath"])
    except ValueError as err:
        return Read(row["id"], error=str(err))
    extraction = bes.extract(root, actions=True)
    return Read(
        row["id"],
        record=_record(resolved, when, extraction, branch="", commit="", commit_date=""),
        extraction=extraction,
    )


def _read_repository(
    row: dict[str, str],
    when: str,
    raw_dir: Path,
    *,
    dry_run: bool,
    allow_schemes: Collection[str],
    runner: gitsource.Runner,
) -> Read:
    kwargs = {"allow_schemes": allow_schemes, "runner": runner}
    commit = row["commit"] or gitsource.resolve_tip(row["location"], row["branch"], **kwargs)
    pinned = {"id": row["id"], "repo_url": row["location"], "commit": commit}
    result = gitsource.sync_git_source(pinned, raw_dir, dry_run=dry_run, **kwargs)
    if result.status == fetch.STATUS_ERROR:
        return Read(row["id"], error=result.detail)
    if dry_run and result.status in (fetch.STATUS_NEW, fetch.STATUS_CHANGED):
        return Read(row["id"], note=f"would read commit {commit[:12]} ({result.detail})")
    checkout = gitsource.checkout_dir(raw_dir, row["id"])
    try:
        root = _inside(checkout, row["subpath"])
    except ValueError as err:
        return Read(row["id"], error=str(err))
    extraction = bes.extract(root, actions=True)
    date = gitsource.commit_date(checkout, **kwargs)
    return Read(
        row["id"],
        record=_record(
            row, when, extraction, branch=row["branch"], commit=commit, commit_date=date
        ),
        extraction=extraction,
    )


def read_source(
    row: dict[str, str],
    raw_dir: Path,
    *,
    dry_run: bool = False,
    allow_schemes: Collection[str] = gitsource.DEFAULT_ALLOWED_SCHEMES,
    runner: gitsource.Runner = subprocess.run,
    clock: Clock = utc_now,
) -> Read:
    """Read one source. A source that cannot be read gives a Read with its error set."""
    try:
        validate_row(row)
    except ValueError as err:
        return Read(row.get("id", "?"), error=str(err))
    when = _stamp(clock())
    try:
        if row["kind"] == "dir":
            return _read_directory(row, when)
        return _read_repository(
            row, when, raw_dir, dry_run=dry_run, allow_schemes=allow_schemes, runner=runner
        )
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as err:
        return Read(row["id"], error=str(err))


def append_read(record: dict, path: Path) -> None:
    """Add one record to the read log: ASCII, one line, never rewriting earlier ones."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n")


def format_read(read: Read) -> str:
    """A short report. It names counts and identifiers, never file contents."""
    if read.error:
        return f"error      {read.source_id}  {read.error}\n"
    if not read.record:
        return f"dry-run    {read.source_id}  {read.note}\n"
    r = read.record
    what = f"commit {r['commit'][:12]} ({r['commit_date']})" if r["commit"] else "directory"
    lines = [
        f"read       {read.source_id}  {what}",
        f"  read at: {r['read_at']}",
        f"  fingerprint: {r['tree_sha256'][:16]}...",
        (
            f"  files: {r['files']} ({r['duplicate_files']} identical copies skipped, "
            f"{r['unparsable_files']} not parseable)"
        ),
        f"  items with relevance: {r['items']}",
    ]
    return "\n".join(lines) + "\n"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.ingest.fixlets",
        description="Read reference fixlets (.bes files) from a directory or a git repository.",
    )
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--source", metavar="ID", help="a source in the untracked source list")
    where.add_argument("--dir", metavar="PATH", help="a local directory, read in place")
    where.add_argument("--git", metavar="URL", help="a git repository to check out and read")
    parser.add_argument("--branch", default="", help="with --git: default is the remote's HEAD")
    parser.add_argument("--commit", default="", help="with --git: default is the branch tip now")
    parser.add_argument("--subpath", default="", help="only read below this relative path")
    parser.add_argument("--sources", type=Path, default=SOURCES_PATH, help="the private list")
    parser.add_argument(
        "--public-sources",
        type=Path,
        default=PUBLIC_SOURCES_PATH,
        help="the tracked list of public sources",
    )
    parser.add_argument(
        "--raw-dir", type=Path, help="default: $BIGFIX_CORPUS_DIR/raw or corpus/raw"
    )
    parser.add_argument("--reads-log", type=Path, help="default: <corpus>/index/" + READS_NAME)
    parser.add_argument(
        "--allow-scheme",
        action="append",
        metavar="SCHEME",
        help="git URL scheme to allow besides https (for example ssh); repeatable",
    )
    parser.add_argument("--dry-run", action="store_true", help="fetch and write nothing")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def _requested_row(args: argparse.Namespace) -> dict[str, str]:
    """The source the command line asks for. Raises ValueError or OSError if unusable."""
    if args.source:
        for candidate in all_sources(args.sources, args.public_sources):
            if candidate["id"] == args.source:
                return candidate
        raise ValueError(f"no source {args.source!r} in {args.sources.name} or the public list")
    row = adhoc_row(
        directory=args.dir,
        repo=args.git,
        branch=args.branch,
        commit=args.commit,
        subpath=args.subpath,
    )
    validate_row(row, "command line")
    return row


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Exit 0 ok, 1 if the source could not be read, 2 for a bad request."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        row = _requested_row(args)
    except (OSError, ValueError) as err:
        # a bad request is a user error; a traceback would only bury the message
        log.error("cannot use that source: %s", err)  # noqa: TRY400
        return 2
    schemes = (*gitsource.DEFAULT_ALLOWED_SCHEMES, *(args.allow_scheme or ()))
    raw_dir = (args.raw_dir or fetch.default_raw_dir()) / RAW_SUBDIR
    read = read_source(row, raw_dir, dry_run=args.dry_run, allow_schemes=schemes)
    sys.stdout.write(format_read(read))
    if read.error:
        return 1
    if read.record and not args.dry_run:
        log_path = args.reads_log or fetch.corpus_base() / "index" / READS_NAME
        append_read(read.record, log_path)
        sys.stdout.write(f"  recorded in {log_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

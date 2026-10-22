"""Build the reference fixlet index from one or more sources.

Reads each source (a directory or a git repository; see fixlets.py), scans what it
found for secrets, runs the offline analyzer over every piece of relevance, and writes
corpus/index/fixlets.sqlite (see fixletindex.py for the schema).

This is company content. The secret scan runs before anything is written:

  - a high-confidence secret in the relevance itself drops that clause;
  - a high-confidence secret in the title, description or an action script leaves that
    one field out of the index (the clause stays) and records which field;
  - warnings (an email address, a `password = value` assignment) are counted and the
    text is kept.

Findings are reported by file, field and rule, never by the matched text, so the
report cannot leak what it found. The scan is a net, not a guarantee, so the index
stays local and untracked whatever it finds.

A source that cannot be read stops the build, and no index is written.

Exit codes: 0 built (or dry run clean), 1 a source could not be read or nothing was
found to index, 2 unusable arguments or source list.

    python -m tools.ingest.fixletbuild --all
    python -m tools.ingest.fixletbuild --source ID [--source ID ...]
    python -m tools.ingest.fixletbuild --dir PATH
    python -m tools.ingest.fixletbuild --git URL [--branch B] [--commit SHA]

Stdout carries the report; diagnostics go to stderr via logging.
"""

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from tools.ingest import (
    bes,
    fetch,
    fixletconventions,
    fixletindex,
    fixlets,
    gitsource,
    scan,
    verdict,
)

log = logging.getLogger("corpus.fixletbuild")

INDEX_NAME = "fixlets.sqlite"
SCAN_ALLOW_PATH = fetch.REPO_CORPUS / "SCAN_ALLOW.csv"

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_USAGE = 2

# an analyzer parse error is stored with its clause; keep it short
PARSE_ERROR_CHARS = 200
# the action scripts of one item, together, as kept in the index
ACTION_EXCERPT_CHARS = bes.ACTION_CHARS
SCRIPT_SEPARATOR = "\n---\n"
# purpose fields whose text can be left out when a credential is found in them
PURPOSE_FIELDS = ("title", "description", "action")


@dataclass
class Exclusion:
    """Where the scan found something. It names the place and the rule, never the text."""

    source_id: str
    path: str
    field: str
    rule: str
    dropped_clause: bool = False


@dataclass
class Assembled:
    data: fixletindex.FixletIndexData
    exclusions: list[Exclusion] = field(default_factory=list)
    warnings: Counter = field(default_factory=Counter)
    dropped: int = 0


def _groups(item: bes.Item) -> list[tuple[str, list[bes.Statement]]]:
    """One group per applicability set, and one per Analysis property."""
    groups: list[tuple[str, list[bes.Statement]]] = []
    applicability = [s for s in item.statements if s.role == "applicability"]
    if applicability:
        groups.append(("applicability", applicability))
    groups += [("property", [s]) for s in item.statements if s.role == "property"]
    return groups


def _scan(
    source_id: str, path: str, text: str, allowed: set[tuple[str, str, str]]
) -> tuple[list[scan.Finding], list[scan.Finding]]:
    """The (blocking, warning) findings in one field's text, after the allowlist."""
    if not text:
        return [], []
    findings = scan.apply_allowlist(scan.scan_text(source_id, path, text), allowed)
    blocking = [f for f in findings if f.severity == scan.BLOCK]
    return blocking, [f for f in findings if f.severity == scan.WARN]


def _clause(
    item: bes.Item,
    role: str,
    statements: list[bes.Statement],
    source_id: str,
    allowed: set[tuple[str, str, str]],
    result: Assembled,
    item_platform: str,
) -> fixletindex.ClauseRow | None:
    lines = tuple(s.relevance for s in statements)
    # a property with no operating-system test of its own takes its analysis's
    platform = fixletconventions.platform_of(lines)
    if platform == fixletconventions.PLATFORM_NONE:
        platform = item_platform
    name = statements[0].name if role == "property" else ""
    purpose = {
        "title": item.title,
        "description": item.description,
        "action": SCRIPT_SEPARATOR.join(item.actions)[:ACTION_EXCERPT_CHARS],
    }
    # the clause itself first: a credential in it cannot be indexed in any form
    body = "\n".join([name, *lines])
    blocking, warnings = _scan(source_id, item.path, body, allowed)
    result.warnings.update(f.rule for f in warnings)
    if blocking:
        result.dropped += 1
        result.exclusions += [
            Exclusion(source_id, item.path, "relevance", f.rule, dropped_clause=True)
            for f in blocking
        ]
        return None
    excluded = []
    for where, text in purpose.items():
        blocking, warnings = _scan(source_id, item.path, text, allowed)
        result.warnings.update(f.rule for f in warnings)
        if blocking:
            excluded.append(where)
            purpose[where] = ""
            result.exclusions += [Exclusion(source_id, item.path, where, f.rule) for f in blocking]
    verdicts = [verdict.analyze_query(line, "client") for line in lines]
    failed = next((v for v in verdicts if not v.parsed), None)
    return fixletindex.ClauseRow(
        source_id=source_id,
        path=item.path,
        file_name=PurePosixPath(item.path).stem,
        content_id=bes.content_id(item.kind, item.title, role, statements),
        item_kind=item.kind,
        role=role,
        property_name=name,
        title=purpose["title"],
        description=purpose["description"],
        action_excerpt=purpose["action"],
        relevance=lines,
        platform=platform,
        parses=failed is None,
        parse_error=(failed.parse_error or failed.crashed)[:PARSE_ERROR_CHARS] if failed else "",
        unknown_inspectors=tuple(sorted({n for v in verdicts for n in v.unknown_inspectors})),
        excluded=tuple(excluded),
    )


def assemble(
    reads: Iterable[fixlets.Read],
    allowed: set[tuple[str, str, str]] | None = None,
    *,
    min_files: int = fixletconventions.MIN_FILES,
    min_share: float | None = None,
) -> Assembled:
    """Turn what was read into index rows, scanning every field first."""
    allowed = allowed or set()
    result = Assembled(
        data=fixletindex.FixletIndexData(
            analyzer_version=verdict.analyzer_version(),
            min_files=min_files,
            min_share=fixletconventions.resolve_min_share(min_share),
        )
    )
    groups: dict[str, str] = {}
    for read in reads:
        if read.extraction is None:
            continue
        record = read.record
        result.data.reads.append(
            fixletindex.ReadRow(
                source_id=record["source_id"],
                kind=record["kind"],
                source_group=record["group"],
                location=record["location"],
                read_at=record["read_at"],
                tree_sha256=record["tree_sha256"],
                branch=record.get("branch", ""),
                commit_sha=record.get("commit", ""),
                commit_date=record.get("commit_date", ""),
                subpath=record.get("subpath", ""),
                files=record["files"],
                duplicate_files=record["duplicate_files"],
                unparsable_files=record["unparsable_files"],
                items=record["items"],
            )
        )
        groups[record["source_id"]] = record["group"]
        for item in read.extraction.items:
            applicability = [s.relevance for s in item.statements if s.role == "applicability"]
            item_platform = fixletconventions.platform_of(applicability)
            for role, statements in _groups(item):
                clause = _clause(
                    item, role, statements, record["source_id"], allowed, result, item_platform
                )
                if clause is not None:
                    result.data.clauses.append(clause)
    result.data.conventions = fixletconventions.mine(
        result.data.clauses, groups, min_files=min_files, min_share=min_share
    )
    return result


def format_report(built: Assembled) -> str:
    clauses = built.data.clauses
    by_role = Counter(c.role for c in clauses)
    lines = [f"clauses: {len(clauses)}"]
    lines += [f"  {role}: {count}" for role, count in sorted(by_role.items())]
    parsing = sum(bool(c.parses) for c in clauses)
    unknown = sum(bool(c.unknown_inspectors) for c in clauses)
    scripts = sum(bool(c.action_excerpt) for c in clauses)
    fields = sum(not e.dropped_clause for e in built.exclusions)
    lines += [
        f"  parse cleanly with the analyzer: {parsing}",
        f"  mention an inspector the analyzer lacks: {unknown}",
        f"  have an action script excerpt: {scripts}",
        f"left out for a credential: {built.dropped} clause(s), {fields} field(s)",
    ]
    for rule, count in sorted(built.warnings.items()):
        lines.append(f"  warning kept: {rule} x{count}")
    for e in built.exclusions:
        what = "clause dropped" if e.dropped_clause else f"{e.field} left out"
        lines.append(f"  {e.source_id}: {e.path}: {what} ({e.rule})")
    return "\n".join(lines) + "\n"


# the pattern kind that says most about each role: guards for applicability, shapes for properties
SHOWN_KIND = {"applicability": "conjunct", "property": "shape"}


def format_conventions(data: fixletindex.FixletIndexData, top: int) -> str:
    """The most common patterns per group and role, across all platforms."""
    lines = []
    for group in sorted({c.source_group for c in data.conventions}):
        for role in fixletindex.ROLES:
            cell = [
                c
                for c in data.conventions
                if (c.source_group, c.role, c.platform, c.kind)
                == (group, role, fixletconventions.PLATFORM_ALL, SHOWN_KIND[role])
            ]
            if not cell:
                continue
            lines.append(f"{group} / {role} ({cell[0].total} items)")
            lines += [f"  {c.files:>5} {c.share:>6.1%}  {c.pattern}" for c in cell[:top]]
    return "\n".join(lines) + "\n" if lines else ""


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.ingest.fixletbuild",
        description="Build corpus/index/fixlets.sqlite from reference fixlet sources.",
    )
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--all", action="store_true", help="every source in the source list")
    where.add_argument("--source", action="append", metavar="ID", help="a listed source")
    where.add_argument("--dir", metavar="PATH", help="a local directory, read in place")
    where.add_argument("--git", metavar="URL", help="a git repository to check out and read")
    parser.add_argument("--branch", default="", help="with --git: default is the remote's HEAD")
    parser.add_argument("--commit", default="", help="with --git: default is the branch tip now")
    parser.add_argument("--subpath", default="", help="only read below this relative path")
    parser.add_argument("--group", default="", help="with --dir or --git: the group it counts in")
    parser.add_argument(
        "--min-files", type=int, default=fixletconventions.MIN_FILES, help="convention support"
    )
    parser.add_argument(
        "--min-share",
        type=float,
        default=None,
        help="convention support as a share of the group, for every role (default: 0.10 for"
        " applicability, 0.01 for property)",
    )
    parser.add_argument(
        "--show-conventions",
        type=int,
        default=0,
        metavar="N",
        help="print each group's top N conventions (company content: local console only)",
    )
    parser.add_argument("--sources", type=Path, default=fixlets.SOURCES_PATH)
    parser.add_argument("--scan-allow", type=Path, default=SCAN_ALLOW_PATH)
    parser.add_argument("--out", type=Path, help="default: <corpus>/index/" + INDEX_NAME)
    parser.add_argument("--raw-dir", type=Path, help="default: $BIGFIX_CORPUS_DIR/raw")
    parser.add_argument("--reads-log", type=Path, help="default: <corpus>/index/fixlet-reads.jsonl")
    parser.add_argument(
        "--allow-scheme",
        action="append",
        metavar="SCHEME",
        help="git URL scheme to allow besides https (for example ssh); repeatable",
    )
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def _requested_rows(args: argparse.Namespace) -> list[dict[str, str]]:
    """The sources the command line asks for. Raises ValueError or OSError if unusable."""
    if args.all or args.source:
        listed = fixlets.read_sources(args.sources)
        if args.all:
            return listed
        by_id = {row["id"]: row for row in listed}
        unknown = [name for name in args.source if name not in by_id]
        if unknown:
            raise ValueError(f"no such source in {args.sources}: {', '.join(unknown)}")
        return [by_id[name] for name in args.source]
    row = fixlets.adhoc_row(
        directory=args.dir,
        repo=args.git,
        branch=args.branch,
        commit=args.commit,
        subpath=args.subpath,
        group=args.group,
    )
    fixlets.validate_row(row, "command line")
    return [row]


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        rows = _requested_rows(args)
        allowed = scan.read_allowlist(args.scan_allow)
    except (OSError, ValueError) as err:
        # a bad request is a user error; a traceback would only bury the message
        log.error("cannot use that request: %s", err)  # noqa: TRY400
        return EXIT_USAGE
    schemes = (*gitsource.DEFAULT_ALLOWED_SCHEMES, *(args.allow_scheme or ()))
    raw_dir = (args.raw_dir or fetch.default_raw_dir()) / fixlets.RAW_SUBDIR
    reads = [
        fixlets.read_source(row, raw_dir, dry_run=args.dry_run, allow_schemes=schemes)
        for row in rows
    ]
    for read in reads:
        sys.stdout.write(fixlets.format_read(read))
    if any(read.error for read in reads):
        sys.stdout.write("not built: a source could not be read\n")
        return EXIT_PROBLEM
    built = assemble(reads, allowed, min_files=args.min_files, min_share=args.min_share)
    sys.stdout.write(format_report(built))
    if args.show_conventions:
        sys.stdout.write(format_conventions(built.data, args.show_conventions))
    if not built.data.clauses:
        sys.stdout.write("nothing to index\n")
        return EXIT_PROBLEM
    if args.dry_run:
        sys.stdout.write("dry run: nothing written\n")
        return EXIT_OK
    out = args.out or fetch.corpus_base() / "index" / INDEX_NAME
    fixletindex.write_index(out, built.data)
    log_path = args.reads_log or fetch.corpus_base() / "index" / fixlets.READS_NAME
    for read in reads:
        fixlets.append_read(read.record, log_path)
    sys.stdout.write(f"wrote {out} (company content; untracked)\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

"""Build the relevance knowledge index from the pinned corpus.

Reads corpus/GIT_SOURCES.csv (git sources, each pinned to a commit) and
corpus/MANIFEST.csv (URL sources, each pinned by SHA-256), converts them to
markdown, scans for secrets, splits them into chunks and examples, and writes
corpus/index/relevance.sqlite (see index.py for the schema).

What counts as a problem:

  error    a source cannot be trusted: the checkout is not at its pinned commit
           or has local modifications, a fetched file does not match its
           manifest hash, a file is not valid UTF-8, or conversion produced no
           text. Any error stops the build and writes no index.
  skipped  a source is not available yet (never fetched, or a converter's
           optional dependency is missing). Reported prominently; the index is
           still built from the rest unless --strict is given.
  blocked  a high-confidence secret was found. No index is written.

Exit codes: 0 built (or dry run clean), 1 errors or nothing to index (or
skipped sources under --strict), 2 unusable arguments or manifests, 3 secret
found.

Converted markdown for URL sources is written to <corpus>/md/<id>.md. Git
sources are read in place; they are already markdown.

Stdout carries the report; diagnostics go to stderr via logging.
"""

import argparse
import hashlib
import logging
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from tools.ingest import chunking, convert, fetch, gitsource, index, scan, verdict

log = logging.getLogger("corpus.build")

INDEX_NAME = "relevance.sqlite"
SCAN_ALLOW_PATH = fetch.REPO_CORPUS / "SCAN_ALLOW.csv"

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_USAGE = 2
EXIT_SECRET = 3

STATUS_INGESTED = "ingested"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

MAX_LISTED_WARNINGS = 15
# an analyzer parse error is stored with its example; keep it short
PARSE_ERROR_CHARS = 200
# below this a chunk could not hold a heading and a line of text
MIN_CHUNK_CHARS = 200


@dataclass(frozen=True)
class SourceRules:
    """How to label a source's documents. Paths are relative to the source's subpath."""

    # (path prefix, dialect); the first matching prefix wins
    dialects: tuple[tuple[str, str], ...] = ()
    default_dialect: str = "unknown"
    # documents under these prefixes are the per-inspector reference ('inspector')
    inspector_prefixes: tuple[str, ...] = ()


# Labelling is specific to how a source lays out its files, so it is keyed by
# source id. A source with no entry gets dialect 'unknown' and kind 'explanation'.
#
# developer.bigfix.com: guide/session and guide/client name their dialect. The
# generated inspector reference (_reference/) does not say whether an inspector
# is session or client, so it is 'unknown' rather than guessed. Everything else
# (core inspectors, tutorial, examples) is shared language material.
SOURCE_RULES: dict[str, SourceRules] = {
    "developer-bigfix-com": SourceRules(
        dialects=(
            ("guide/session/", "session"),
            ("guide/client/", "client"),
            ("tutorial/client/", "client"),
            ("_reference/", "unknown"),
        ),
        default_dialect="both",
        inspector_prefixes=("_reference/docs/",),
    ),
}


def dialect_for(rules: SourceRules, relative_path: str) -> str:
    for prefix, dialect in rules.dialects:
        if relative_path.startswith(prefix):
            return dialect
    return rules.default_dialect


def kind_for(rules: SourceRules, relative_path: str) -> str:
    if any(relative_path.startswith(prefix) for prefix in rules.inspector_prefixes):
        return "inspector"
    return "explanation"


@dataclass
class Document:
    path: str  # relative, posix separators
    text: str


class SourceProblem(Exception):  # noqa: N818 - carries a status, not only an error
    """A source that cannot be ingested; `status` is skipped or error."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class SourceReport:
    id: str
    kind: str  # git or url
    status: str = STATUS_INGESTED
    detail: str = ""
    files: int = 0
    chunks: int = 0
    examples: int = 0
    duplicate_examples: int = 0
    stub_sections: int = 0
    empty_sections: int = 0
    leftover_templates: int = 0
    examples_unparsed: int = 0  # indexed examples the analyzer could not parse
    examples_relabelled: int = 0  # unknown-dialect examples the analyzer labelled
    skipped_files: Counter = field(default_factory=Counter)


@dataclass
class BuildResult:
    reports: list[SourceReport]
    findings: list[scan.Finding]
    data: index.IndexData
    exit_code: int
    wrote_index: bool = False


@dataclass
class _Loaded:
    source: index.SourceRow
    documents: list[Document]
    rules: SourceRules | None
    skipped_files: Counter


def _load_git_source(row: dict[str, str], raw_dir: Path) -> _Loaded:
    source_id = row["id"]
    checkout = gitsource.checkout_dir(raw_dir, source_id)
    if not (checkout / ".git").is_dir():
        raise SourceProblem(
            STATUS_SKIPPED, f"no checkout at {checkout}; run python -m tools.ingest.gitsource"
        )
    try:
        head = gitsource.head_commit(checkout)
        clean = gitsource.is_clean(checkout)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as err:
        raise SourceProblem(STATUS_ERROR, f"cannot inspect the checkout: {err}") from err
    if head != row["commit"]:
        raise SourceProblem(
            STATUS_ERROR,
            f"checkout is at {head[:12]} but the pinned commit is {row['commit'][:12]}; "
            "run python -m tools.ingest.gitsource",
        )
    if not clean:
        raise SourceProblem(
            STATUS_ERROR, "checkout has local modifications; it no longer matches the pin"
        )
    root = checkout / row["subpath"]
    if not root.is_dir() or not root.resolve().is_relative_to(checkout.resolve()):
        raise SourceProblem(
            STATUS_ERROR, f"subpath {row['subpath']!r} is not a directory in the checkout"
        )

    documents: list[Document] = []
    skipped: Counter = Counter()
    found = sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix())
    for path in found:
        if path.is_dir() and not path.is_symlink():
            continue  # only files are counted; their own paths decide what happens to them
        relative = path.relative_to(root).as_posix()
        if any(part.startswith(".") for part in relative.split("/")):
            skipped["hidden"] += 1
        elif path.is_symlink():
            skipped["symlink"] += 1
        elif path.is_file():
            if path.suffix.lower() != ".md":
                skipped[path.suffix.lower() or "(no extension)"] += 1
                continue
            try:
                documents.append(Document(relative, path.read_text(encoding="utf-8-sig")))
            except UnicodeDecodeError as err:
                raise SourceProblem(
                    STATUS_ERROR, f"{relative}: not valid UTF-8 ({err.reason})"
                ) from err
    rules = SOURCE_RULES.get(source_id, SourceRules())
    source = index.SourceRow(
        id=source_id,
        kind="git",
        origin=row["repo_url"],
        revision=row["commit"],
        license=row.get("license", ""),
        dialect=rules.default_dialect,
    )
    return _Loaded(source, documents, rules, skipped)


def _to_markdown(target: Path, data: bytes) -> str:
    """Convert a fetched file to markdown by its extension."""
    suffix = target.suffix.lower()
    if suffix in (".html", ".htm"):
        return convert.html_to_markdown(data.decode("utf-8", errors="replace"))
    if suffix == ".pdf":
        return convert.pdf_to_markdown(target)
    if suffix in (".md", ".markdown", ".txt"):
        return data.decode("utf-8-sig")
    raise SourceProblem(
        STATUS_SKIPPED, f"no converter for {suffix or 'files without an extension'}"
    )


def _load_url_source(row: dict[str, str], raw_dir: Path, md_dir: Path, *, dry_run: bool) -> _Loaded:
    source_id = row["id"].strip()
    expected = row.get("sha256", "").strip().lower()
    if not expected:
        raise SourceProblem(
            STATUS_SKIPPED,
            "not fetched yet (no sha256 in the manifest); run python -m tools.ingest.fetch",
        )
    dialect = row.get("dialect", "").strip() or "unknown"
    if dialect not in index.DIALECTS:
        raise SourceProblem(
            STATUS_ERROR, f"dialect {dialect!r} is not one of {', '.join(index.DIALECTS)}"
        )
    try:
        relative, target = fetch.raw_path_for(row, raw_dir)
    except ValueError as err:
        raise SourceProblem(STATUS_ERROR, str(err)) from err
    if not target.is_file():
        raise SourceProblem(
            STATUS_SKIPPED, f"raw file {relative} is missing; run python -m tools.ingest.fetch"
        )
    data = target.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise SourceProblem(
            STATUS_ERROR,
            f"{relative} does not match the manifest sha256; re-run python -m tools.ingest.fetch",
        )
    try:
        markdown = _to_markdown(target, data)
    except SourceProblem:
        raise
    except convert.ConverterUnavailableError as err:
        raise SourceProblem(STATUS_SKIPPED, str(err)) from err
    except UnicodeDecodeError as err:
        raise SourceProblem(STATUS_ERROR, f"{relative}: not valid UTF-8 ({err.reason})") from err
    except Exception as err:
        # a third-party parser failing on untrusted input is a bad source, not a crash
        raise SourceProblem(
            STATUS_ERROR, f"conversion failed: {type(err).__name__}: {err}"
        ) from err
    if not markdown.strip():
        raise SourceProblem(STATUS_ERROR, "conversion produced no text")
    document = Document(f"{source_id}.md", markdown)
    if not dry_run:
        fetch.atomic_write(md_dir / document.path, markdown.encode("utf-8"))
    source = index.SourceRow(
        id=source_id,
        kind="url",
        origin=row["source_url"].strip(),
        revision=expected,
        license=row.get("license", "").strip(),
        version=row.get("bigfix_version", "").strip(),
        dialect=dialect,
    )
    return _Loaded(source, [document], None, Counter())


def _example_context(path: tuple[str, ...], example: chunking.Example) -> str:
    return " ".join(part for part in (" > ".join(path), example.before, example.after) if part)


def build(
    git_rows: Sequence[dict[str, str]],
    url_rows: Sequence[dict[str, str]],
    *,
    raw_dir: Path,
    md_dir: Path,
    index_path: Path,
    allowed: set[tuple[str, str, str]] | None = None,
    dry_run: bool = False,
    only: Sequence[str] | None = None,
    strict: bool = False,
    max_chunk_chars: int = chunking.MAX_CHUNK_CHARS,
    built_at: datetime | None = None,
) -> BuildResult:
    """Run the whole pipeline. See the module docstring for statuses and exit codes."""
    wanted = [("git", row) for row in git_rows] + [("url", row) for row in url_rows]
    if only:
        wanted = [(kind, row) for kind, row in wanted if row["id"].strip() in only]

    reports: list[SourceReport] = []
    loaded: list[_Loaded] = []
    for kind, row in wanted:
        report = SourceReport(id=row["id"].strip(), kind=kind)
        reports.append(report)
        try:
            item = (
                _load_git_source(row, raw_dir)
                if kind == "git"
                else _load_url_source(row, raw_dir, md_dir, dry_run=dry_run)
            )
        except SourceProblem as problem:
            report.status, report.detail = problem.status, problem.detail
            continue
        report.files = len(item.documents)
        report.skipped_files = item.skipped_files
        loaded.append(item)

    findings: list[scan.Finding] = []
    for item in loaded:
        for document in item.documents:
            findings.extend(scan.scan_text(item.source.id, document.path, document.text))
    findings = scan.apply_allowlist(findings, allowed or set())

    data = index.IndexData(analyzer_version=verdict.analyzer_version())
    if any(f.severity == scan.BLOCK for f in findings):
        return BuildResult(reports, findings, data, EXIT_SECRET)

    seen_examples: set[tuple] = set()
    # the same query analysed under the same pin gives the same verdict
    verdicts: dict[tuple[str, str], verdict.Verdict] = {}
    by_id = {report.id: report for report in reports}
    for item in loaded:
        report = by_id[item.source.id]
        data.sources.append(item.source)
        for document in item.documents:
            rules = item.rules
            dialect = dialect_for(rules, document.path) if rules else item.source.dialect
            kind = kind_for(rules, document.path) if rules else "explanation"
            parsed = chunking.parse_document(
                document.text,
                default_title=PurePosixPath(document.path).stem,
                max_chars=max_chunk_chars,
            )
            report.stub_sections += parsed.stub_skipped
            report.empty_sections += parsed.empty_skipped
            report.leftover_templates += parsed.leftover_templates
            for chunk in parsed.chunks:
                row = index.ChunkRow(
                    source_id=item.source.id,
                    path=document.path,
                    heading=chunk.heading,
                    section_path=chunk.section_path,
                    kind=kind,
                    dialect=dialect,
                    version=item.source.version,
                    text=chunk.text,
                )
                for example in chunk.examples:
                    pinned = dialect if dialect in verdict.PINNABLE else ""
                    found = verdicts.get((example.query, pinned))
                    if found is None:
                        found = verdicts[(example.query, pinned)] = verdict.analyze_query(
                            example.query, pinned
                        )
                    # The analyzer labels only what no source states, and only when it
                    # is definite and consistent; a stated dialect is never overridden.
                    example_dialect, dialect_source = dialect, "source"
                    if dialect == "unknown":
                        label = verdict.inferred_dialect(found)
                        if label:
                            example_dialect, dialect_source = label, "analyzer"
                    key = (example_dialect, example.query, example.answers, example.errors)
                    if key in seen_examples:
                        report.duplicate_examples += 1
                        continue
                    seen_examples.add(key)
                    judged = not found.crashed
                    report.examples_relabelled += dialect_source == "analyzer"
                    report.examples_unparsed += judged and not found.parsed
                    row.examples.append(
                        index.ExampleRow(
                            form=example.form,
                            query=example.query,
                            answers=example.answers,
                            errors=example.errors,
                            result_type=example.result_type,
                            incomplete=example.incomplete,
                            context=_example_context(chunk.section_path, example),
                            dialect=example_dialect,
                            dialect_source=dialect_source,
                            parses=found.parsed if judged else None,
                            parse_error=(found.parse_error or found.crashed)[:PARSE_ERROR_CHARS],
                            unknown_inspectors=found.unknown_inspectors,
                        )
                    )
                data.chunks.append(row)
                report.chunks += 1
                report.examples += len(row.examples)

    has_error = any(r.status == STATUS_ERROR for r in reports)
    has_skipped = any(r.status == STATUS_SKIPPED for r in reports)
    if has_error or (strict and has_skipped) or not data.sources:
        return BuildResult(reports, findings, data, EXIT_PROBLEM)
    if not dry_run:
        index.write_index(index_path, data, built_at=built_at)
    return BuildResult(reports, findings, data, EXIT_OK, wrote_index=not dry_run)


def format_report(result: BuildResult, index_path: Path, *, dry_run: bool) -> str:
    lines: list[str] = []
    for report in result.reports:
        head = f"{report.kind:<4} {report.id:<34} {report.status:<9}"
        if report.status == STATUS_INGESTED:
            extra = (
                f" files={report.files} chunks={report.chunks} examples={report.examples}"
                f" duplicate-examples={report.duplicate_examples}"
                f" stub-sections={report.stub_sections} empty-sections={report.empty_sections}"
                f" template-leftovers={report.leftover_templates}"
                f" examples-unparsed={report.examples_unparsed}"
                f" dialect-labelled={report.examples_relabelled}"
            )
            lines.append(head + extra)
            if report.skipped_files:
                parts = ", ".join(f"{k}={v}" for k, v in sorted(report.skipped_files.items()))
                lines.append(f"     files not indexed: {parts}")
        else:
            lines.append(f"{head} {report.detail}")
    blocking = [f for f in result.findings if f.severity == scan.BLOCK]
    warnings = [f for f in result.findings if f.severity == scan.WARN]
    lines.append(f"scan: {len(blocking)} blocking, {len(warnings)} warnings")
    for finding in blocking:
        lines.append(f"  BLOCK {finding.rule}  {finding.source_id}:{finding.path}:{finding.line}")
    for finding in warnings[:MAX_LISTED_WARNINGS]:
        lines.append(f"  warn  {finding.rule}  {finding.source_id}:{finding.path}:{finding.line}")
    if len(warnings) > MAX_LISTED_WARNINGS:
        lines.append(f"  ... and {len(warnings) - MAX_LISTED_WARNINGS} more warnings")
    if result.wrote_index:
        lines.append(
            f"index written to {index_path}: {len(result.data.chunks)} chunks, "
            f"{result.data.example_count} examples from {len(result.data.sources)} sources"
        )
    elif result.exit_code == EXIT_OK and dry_run:
        lines.append(
            f"dry run: would write {len(result.data.chunks)} chunks, "
            f"{result.data.example_count} examples from {len(result.data.sources)} sources"
        )
    else:
        lines.append("index NOT written")
    return "\n".join(lines) + "\n"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    base = fetch.corpus_base()
    parser = argparse.ArgumentParser(
        prog="python -m tools.ingest.build",
        description="Build the relevance knowledge index from the pinned corpus.",
    )
    parser.add_argument("--dry-run", action="store_true", help="do everything except write files")
    parser.add_argument(
        "--only", action="append", metavar="ID", help="limit to a source id (needs --dry-run)"
    )
    parser.add_argument("--strict", action="store_true", help="treat skipped sources as failures")
    parser.add_argument("--git-sources", type=Path, default=gitsource.GIT_SOURCES_PATH)
    parser.add_argument("--manifest", type=Path, default=fetch.REPO_CORPUS / "MANIFEST.csv")
    parser.add_argument("--scan-allow", type=Path, default=SCAN_ALLOW_PATH)
    parser.add_argument("--raw-dir", type=Path, default=base / "raw")
    parser.add_argument("--md-dir", type=Path, default=base / "md")
    parser.add_argument("--index", type=Path, default=base / "index" / INDEX_NAME)
    parser.add_argument("--max-chunk-chars", type=int, default=chunking.MAX_CHUNK_CHARS)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if args.only and not args.dry_run:
        # a partial build would overwrite a complete index with a subset
        log.error("--only is only allowed with --dry-run")
        return EXIT_USAGE
    if args.max_chunk_chars < MIN_CHUNK_CHARS:
        log.error("--max-chunk-chars must be at least %d", MIN_CHUNK_CHARS)
        return EXIT_USAGE
    try:
        git_rows = gitsource.read_git_sources(args.git_sources) if args.git_sources.exists() else []
        _, url_rows = fetch.read_manifest(args.manifest)
        allowed = scan.read_allowlist(args.scan_allow)
    except (OSError, ValueError) as err:
        # a bad manifest is a user error; a traceback would only bury the message
        log.error("cannot read the corpus manifests: %s", err)  # noqa: TRY400
        return EXIT_USAGE
    ids = [row["id"].strip() for row in [*git_rows, *url_rows]]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        log.error("source id used in both manifests: %s", ", ".join(duplicates))
        return EXIT_USAGE

    result = build(
        git_rows,
        url_rows,
        raw_dir=args.raw_dir,
        md_dir=args.md_dir,
        index_path=args.index,
        allowed=allowed,
        dry_run=args.dry_run,
        only=args.only,
        strict=args.strict,
        max_chunk_chars=args.max_chunk_chars,
    )
    sys.stdout.write(format_report(result, args.index, dry_run=args.dry_run))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())

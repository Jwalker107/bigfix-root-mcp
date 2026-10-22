"""Run the offline relevance analyzer over every example in the knowledge index.

For each example the analyzer parses the query, classifies its dialect and
flags unknown inspectors, with no BigFix contact at all. The point is to measure
two things at once:

- how trustworthy the documented examples are as models of valid relevance; and
- how well the analyzer's inspector snapshot agrees with the documentation.

A parse failure on an example that documents an error is not a defect (the
example is showing what goes wrong), so it is counted separately. An example
whose dialect the source states is analysed with that dialect pinned; the others
are analysed unpinned, and how the analyzer classifies them is itself a result.

This is a measurement, not a gate: the exit status is 0 unless the index cannot
be read. Results never leave the machine.

    python -m tools.eval.analyze_examples [--index PATH] [--csv PATH]
"""

import argparse
import csv
import json
import logging
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tools.ingest import fetch, verdict

log = logging.getLogger("corpus.eval")

INDEX_NAME = "relevance.sqlite"
TOP_UNKNOWN_INSPECTORS = 15
TOP_FINDING_CODES = 10
CSV_QUERY_CHARS = 200


@dataclass
class ExampleRow:
    """One example as stored in the index."""

    example_id: int
    source_id: str
    path: str
    dialect: str
    form: str
    query: str
    documents_error: bool
    incomplete: bool


@dataclass
class Result:
    """What the analyzer made of one example."""

    row: ExampleRow
    pinned: str  # 'session', 'client' or '' when the source does not say
    parsed: bool = False
    parse_error: str = ""
    effective: str = ""  # the dialect the analyzer settled on
    classified: str = ""
    resolved: str = ""
    mismatch: bool = False  # the query's own evidence contradicts the pinned dialect
    findings: list[tuple[str, str]] = field(default_factory=list)  # (code, severity)
    unknown_inspectors: list[str] = field(default_factory=list)
    crashed: str = ""  # the analyzer itself raised; never expected


def load_examples(index_path: Path) -> list[ExampleRow]:
    """Read every example from the index (read-only).

    An example's dialect counts only when its source stated it. One the build
    labelled from this same analyzer is reported as 'unknown': pinning it to its
    own inferred dialect would make the mismatch check circular.
    """
    connection = sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT id, source_id, path, dialect, form, query, errors, incomplete, dialect_source "
            "FROM examples ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    return [
        ExampleRow(
            example_id=row[0],
            source_id=row[1],
            path=row[2],
            dialect=row[3] if row[8] == "source" else "unknown",
            form=row[4],
            query=row[5],
            documents_error=bool(json.loads(row[6])),
            incomplete=bool(row[7]),
        )
        for row in rows
    ]


def analyze_example(row: ExampleRow) -> Result:
    """Analyze one example with its dialect pinned when the source states it."""
    pinned = row.dialect if row.dialect in verdict.PINNABLE else ""
    found = verdict.analyze_query(row.query, pinned)
    return Result(
        row=row,
        pinned=pinned,
        parsed=found.parsed,
        parse_error=found.parse_error,
        effective=found.effective,
        classified=found.classified,
        resolved=found.resolved,
        mismatch=found.mismatch,
        findings=list(found.findings),
        unknown_inspectors=list(found.unknown_inspectors),
        crashed=found.crashed,
    )


def analyze_all(rows: Iterable[ExampleRow]) -> list[Result]:
    return [analyze_example(row) for row in rows]


def _pct(part: int, whole: int) -> str:
    return "n/a" if whole == 0 else f"{100 * part / whole:.1f}%"


def summarize(results: Sequence[Result]) -> dict:
    """Aggregate the results into plain data (also what the report prints)."""
    parsed = [r for r in results if r.parsed]
    failed = [r for r in results if not r.parsed and not r.crashed]
    failed_expected = [r for r in failed if r.row.documents_error]
    failed_unexpected = [r for r in failed if not r.row.documents_error]
    with_unknown = [r for r in results if r.unknown_inspectors]
    unknown_by_name: Counter = Counter()
    for r in with_unknown:
        unknown_by_name.update(set(r.unknown_inspectors))
    findings: Counter = Counter()
    for r in results:
        findings.update({code for code, _ in r.findings})
    by_dialect: dict[str, dict] = {}
    for dialect in sorted({r.row.dialect for r in results}):
        group = [r for r in results if r.row.dialect == dialect]
        by_dialect[dialect] = {
            "total": len(group),
            "parsed": sum(r.parsed for r in group),
            "with_unknown_inspectors": sum(bool(r.unknown_inspectors) for r in group),
            "mismatch": sum(r.mismatch for r in group),
        }
    unpinned = [r for r in results if not r.pinned and r.parsed]
    return {
        "total": len(results),
        "parsed": len(parsed),
        "parse_failed": len(failed),
        "parse_failed_documented_error": len(failed_expected),
        "parse_failed_unexpected": len(failed_unexpected),
        "crashed": sum(bool(r.crashed) for r in results),
        "with_unknown_inspectors": len(with_unknown),
        "distinct_unknown_inspectors": len(unknown_by_name),
        "top_unknown_inspectors": unknown_by_name.most_common(TOP_UNKNOWN_INSPECTORS),
        "finding_codes": findings.most_common(TOP_FINDING_CODES),
        "dialect_mismatch": sum(r.mismatch for r in results),
        "by_source_dialect": by_dialect,
        "unpinned_classification": dict(Counter(r.effective for r in unpinned)),
        "incomplete_examples": sum(r.row.incomplete for r in results),
        "unexpected_failures": [
            (r.row.path, r.row.query[:90], r.parse_error[:90]) for r in failed_unexpected
        ],
    }


def format_report(summary: dict, *, sample: int = 10) -> str:
    total = summary["total"]
    unknown, names = summary["with_unknown_inspectors"], summary["distinct_unknown_inspectors"]
    lines = [
        f"examples analysed: {total}",
        f"  parsed cleanly:   {summary['parsed']} ({_pct(summary['parsed'], total)})",
        f"  failed to parse:  {summary['parse_failed']} ({_pct(summary['parse_failed'], total)})",
        f"    document an error themselves (expected): {summary['parse_failed_documented_error']}",
        f"    document no error (unexpected):          {summary['parse_failed_unexpected']}",
        f"  analyzer crashed: {summary['crashed']}",
        f"  mention an inspector the analyzer does not know: {unknown} ({names} distinct names)",
        f"  contradict their pinned dialect: {summary['dialect_mismatch']}",
        f"  marked incomplete (elided answers): {summary['incomplete_examples']}",
        "",
        "by the dialect the source states:",
    ]
    for dialect, group in summary["by_source_dialect"].items():
        rate = _pct(group["parsed"], group["total"])
        unknown_here, mismatch_here = group["with_unknown_inspectors"], group["mismatch"]
        lines.append(
            f"  {dialect:8} total={group['total']:5} parsed={group['parsed']:5} ({rate:>6}) "
            f"unknown-inspectors={unknown_here:4} mismatch={mismatch_here:3}"
        )
    lines += ["", "how the analyzer classifies examples whose dialect the source does not pin:"]
    for dialect, count in sorted(summary["unpinned_classification"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {dialect or '(none)':10} {count}")
    lines += ["", "lint findings (examples carrying each code):"]
    lines += [f"  {code:28} {count}" for code, count in summary["finding_codes"]] or ["  (none)"]
    lines += ["", f"most common unknown inspectors (top {TOP_UNKNOWN_INSPECTORS}):"]
    lines += [f"  {count:4}  {name}" for name, count in summary["top_unknown_inspectors"]] or [
        "  (none)"
    ]
    unexpected = summary["unexpected_failures"]
    lines += [
        "",
        f"unexpected parse failures (first {min(sample, len(unexpected))} of {len(unexpected)}):",
    ]
    for path, query, error in unexpected[:sample]:
        lines.append(f"  {path}: {query!r} -> {error}")
    return "\n".join(lines) + "\n"


def write_csv(results: Iterable[Result], path: Path) -> None:
    """One row per example, for digging into individual cases."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                "id", "source", "path", "dialect", "form", "documents_error", "parsed",
                "effective_dialect", "classified", "resolved", "mismatch", "finding_codes",
                "unknown_inspectors", "parse_error", "query",
            ]
        )  # fmt: skip
        for r in results:
            writer.writerow(
                [
                    r.row.example_id, r.row.source_id, r.row.path, r.row.dialect, r.row.form,
                    int(r.row.documents_error), int(r.parsed), r.effective, r.classified,
                    r.resolved, int(r.mismatch), " ".join(sorted({c for c, _ in r.findings})),
                    " | ".join(r.unknown_inspectors), r.parse_error[:CSV_QUERY_CHARS],
                    r.row.query[:CSV_QUERY_CHARS],
                ]
            )  # fmt: skip


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.eval.analyze_examples",
        description="Run the offline analyzer over every example in the knowledge index.",
    )
    parser.add_argument(
        "--index", type=Path, default=fetch.corpus_base() / "index" / INDEX_NAME, help="index file"
    )
    parser.add_argument("--csv", type=Path, help="also write one row per example to this file")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    # a query can contain characters the console cannot print; never fail on that
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    try:
        rows = load_examples(args.index)
    except (OSError, sqlite3.Error) as err:
        # a missing or unreadable index is a user error; a traceback would only bury the message
        log.error("cannot read examples from %s: %s", args.index, err)  # noqa: TRY400
        return 2
    results = analyze_all(rows)
    sys.stdout.write(format_report(summarize(results)))
    if args.csv:
        write_csv(results, args.csv)
        sys.stdout.write(f"per-example results written to {args.csv}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Select the golden set of examples from the knowledge index.

The golden set is the examples a live check can fairly be held to. An example is
in it when all of these hold:

  - it is a documented Q:/A: example (form `qna` or `fence-qa`); a bare relevance
    fence has no expected output to compare with;
  - the offline analyzer parses it; a query that does not parse is not a model of
    valid relevance;
  - it is not marked incomplete; an elided answer list cannot be compared exactly;
  - it documents no error; this set is for answers (error examples are a
    different check);
  - it documents at least one answer.

Each excluded example is counted under the first rule it fails, in the order
above, so the summary says where the rest went. An example that mentions an
inspector the analyzer's snapshot lacks is kept and flagged, because that is as
likely to be newer documentation as a mistake.

The examples are documentation text, so the set is written into the untracked
corpus directory (corpus/eval/) from the index each time. Only this code and its
criteria are committed; the data is reproducible and is not redistributed.

    python -m tools.eval.golden [--index PATH] [--out PATH]
"""

import argparse
import json
import logging
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tools.ingest import fetch

log = logging.getLogger("corpus.eval")

INDEX_NAME = "relevance.sqlite"
GOLDEN_NAME = "golden.jsonl"
META_NAME = "golden.meta.json"
# bump when the selection rules above change, so an old file is recognisable
CRITERIA_VERSION = 1
ANSWER_FORMS = frozenset({"qna", "fence-qa"})

# first failing rule wins; the order is part of the contract (see the docstring)
REASONS = (
    "bare-fence",
    "not-analysed",
    "does-not-parse",
    "incomplete",
    "documents-error",
    "no-documented-answer",
)


@dataclass(frozen=True)
class Candidate:
    """An example as stored in the index, with the fields selection needs."""

    example_id: int
    source_id: str
    path: str
    dialect: str
    dialect_source: str
    form: str
    query: str
    answers: tuple[str, ...]
    errors: tuple[str, ...]
    result_type: str
    incomplete: bool
    parses: bool | None
    unknown_inspectors: tuple[str, ...]


@dataclass
class Selection:
    selected: list[Candidate]
    excluded: Counter


def load_candidates(index_path: Path) -> tuple[list[Candidate], dict[str, str], list[dict]]:
    """Read every example, the index's meta table and its sources (read-only)."""
    connection = sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT id, source_id, path, dialect, dialect_source, form, query, answers, errors,"
            " result_type, incomplete, parses, unknown_inspectors FROM examples ORDER BY id"
        ).fetchall()
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        sources = [
            {"id": r[0], "kind": r[1], "origin": r[2], "revision": r[3]}
            for r in connection.execute(
                "SELECT id, kind, origin, revision FROM sources ORDER BY id"
            )
        ]
    finally:
        connection.close()
    candidates = [
        Candidate(
            example_id=row[0],
            source_id=row[1],
            path=row[2],
            dialect=row[3],
            dialect_source=row[4],
            form=row[5],
            query=row[6],
            answers=tuple(json.loads(row[7])),
            errors=tuple(json.loads(row[8])),
            result_type=row[9],
            incomplete=bool(row[10]),
            parses=None if row[11] is None else bool(row[11]),
            unknown_inspectors=tuple(json.loads(row[12])),
        )
        for row in rows
    ]
    return candidates, meta, sources


def exclusion_reason(example: Candidate) -> str | None:
    """The first rule the example fails (one of REASONS), or None when it qualifies."""
    if example.form not in ANSWER_FORMS:
        return "bare-fence"
    if example.parses is None:
        return "not-analysed"
    if not example.parses:
        return "does-not-parse"
    if example.incomplete:
        return "incomplete"
    if example.errors:
        return "documents-error"
    if not example.answers:
        return "no-documented-answer"
    return None


def select(candidates: Iterable[Candidate]) -> Selection:
    """Split the examples into the golden set (in index order) and a count of why not."""
    selected: list[Candidate] = []
    excluded: Counter = Counter()
    for example in candidates:
        reason = exclusion_reason(example)
        if reason is None:
            selected.append(example)
        else:
            excluded[reason] += 1
    return Selection(selected, excluded)


def record(example: Candidate) -> dict:
    """The JSON object written for one golden example."""
    return {
        "id": example.example_id,
        "source": example.source_id,
        "path": example.path,
        "dialect": example.dialect,
        "dialect_source": example.dialect_source,
        "form": example.form,
        "query": example.query,
        "answers": list(example.answers),
        "result_type": example.result_type,
        "unknown_inspectors": list(example.unknown_inspectors),
    }


def summarize(selection: Selection, total: int) -> dict:
    chosen = selection.selected
    return {
        "examples_in_index": total,
        "golden": len(chosen),
        "excluded": {reason: selection.excluded.get(reason, 0) for reason in REASONS},
        "by_dialect": dict(Counter(e.dialect for e in chosen)),
        "by_dialect_source": dict(Counter(e.dialect_source for e in chosen)),
        "by_form": dict(Counter(e.form for e in chosen)),
        "by_source": dict(Counter(e.source_id for e in chosen)),
        "with_unknown_inspectors": sum(bool(e.unknown_inspectors) for e in chosen),
    }


def format_report(summary: dict) -> str:
    lines = [
        f"examples in the index: {summary['examples_in_index']}",
        f"golden set:            {summary['golden']}",
        "",
        "excluded, by the first rule each example fails:",
    ]
    lines += [f"  {reason:22} {count}" for reason, count in summary["excluded"].items()]
    for title, key in (
        ("by dialect", "by_dialect"),
        ("by how the dialect was set", "by_dialect_source"),
        ("by form", "by_form"),
        ("by source", "by_source"),
    ):
        lines += ["", f"golden set {title}:"]
        lines += [f"  {name:22} {count}" for name, count in sorted(summary[key].items())] or [
            "  (none)"
        ]
    unknown = summary["with_unknown_inspectors"]
    lines += ["", f"golden examples using an inspector the analyzer lacks: {unknown}"]
    return "\n".join(lines) + "\n"


def _jsonl(selection: Selection) -> bytes:
    # ASCII-only and key-sorted, so the same index always gives the same bytes
    lines = [json.dumps(record(e), sort_keys=True, ensure_ascii=True) for e in selection.selected]
    return ("\n".join(lines) + "\n").encode("ascii") if lines else b""


def _meta(summary: dict, meta: dict[str, str], sources: list[dict]) -> bytes:
    document = {
        "criteria_version": CRITERIA_VERSION,
        "index_schema_version": meta.get("schema_version"),
        "index_built_at": meta.get("built_at"),
        "analyzer_version": meta.get("analyzer_version"),
        "sources": sources,
        "summary": summary,
    }
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode(
        "ascii"
    )


def write_golden(
    selection: Selection, summary: dict, meta: dict, sources: list[dict], out: Path
) -> Path:
    """Write the JSONL file and its meta sidecar; returns the sidecar's path."""
    fetch.atomic_write(out, _jsonl(selection))
    sidecar = out.with_name(META_NAME)
    fetch.atomic_write(sidecar, _meta(summary, meta, sources))
    return sidecar


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    base = fetch.corpus_base()
    parser = argparse.ArgumentParser(
        prog="python -m tools.eval.golden",
        description="Select the golden set of examples from the knowledge index.",
    )
    parser.add_argument("--index", type=Path, default=base / "index" / INDEX_NAME)
    parser.add_argument("--out", type=Path, default=base / "eval" / GOLDEN_NAME)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        candidates, meta, sources = load_candidates(args.index)
    except (OSError, sqlite3.Error) as err:
        # a missing or old-schema index is a user error; say how to fix it
        log.error(  # noqa: TRY400
            "cannot read examples from %s (%s); rebuild it with python -m tools.ingest.build",
            args.index,
            err,
        )
        return 2
    selection = select(candidates)
    summary = summarize(selection, len(candidates))
    sidecar = write_golden(selection, summary, meta, sources, args.out)
    sys.stdout.write(format_report(summary))
    sys.stdout.write(f"written to {args.out} (details in {sidecar.name})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

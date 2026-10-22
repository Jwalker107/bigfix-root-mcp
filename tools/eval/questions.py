"""Collect candidate questions from real BigFix content.

Fixlets, tasks and analyses written by a team are the best available record of
what people actually need relevance for. This reads a directory of `.bes` files
and writes one candidate per piece of relevance, with the title and description
that say what it was for, so that real questions can be drafted from them.

  applicability  a Fixlet or Task: all its <Relevance> lines together decide which
                 computers it applies to. One candidate per item.
  property       an Analysis <Property>: relevance that reports a value. One
                 candidate per property, with the analysis title as context.

Confidentiality comes first. This is company content, so:

  - the output goes to the untracked corpus/eval/ directory and is never meant for
    the repository; only this code is committed;
  - the XML parser resolves no entities and loads no DTD or network resource, so a
    hostile file cannot read other files;
  - every candidate is scanned for secrets (the same rules as the corpus build); one
    holding a high-confidence secret is left out of the output and only counted.

Action scripts are not read. Only relevance elements are, so what is written is the
relevance, its title and its description.

    python -m tools.eval.questions extract --source DIR [--out PATH]
"""

import argparse
import hashlib
import json
import logging
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

from tools.ingest import fetch, scan, verdict
from tools.ingest.bes import Extraction, Item, Statement, extract

log = logging.getLogger("corpus.eval")

CANDIDATES_NAME = "candidates.jsonl"


def _candidate_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def _record(item: Item, statements: list[Statement], role: str) -> dict:
    relevance = [s.relevance for s in statements]
    verdicts = [verdict.analyze_query(text, "client") for text in relevance]
    return {
        "id": _candidate_id(
            item.kind, item.title, role, *(s.name + s.relevance for s in statements)
        ),
        "kind": role,
        "item_kind": item.kind,
        "title": item.title,
        "name": statements[0].name if role == "property" else "",
        "description": item.description,
        "source_path": item.path,
        "relevance": relevance,
        "dialect": "client",  # BES content relevance is evaluated on the client
        "parses": all(v.parsed for v in verdicts),
        "unknown_inspectors": sorted({name for v in verdicts for name in v.unknown_inspectors}),
    }


def candidates(items: Iterable[Item]) -> tuple[list[dict], Counter]:
    """One candidate per Fixlet/Task, and one per Analysis property.

    A candidate with a high-confidence secret in its text is dropped and counted;
    warnings (an email address, say) are counted and the candidate is kept.
    """
    records: list[dict] = []
    stats: Counter = Counter()
    for item in items:
        groups: list[tuple[str, list[Statement]]] = []
        applicability = [s for s in item.statements if s.role == "applicability"]
        if applicability:
            groups.append(("applicability", applicability))
        groups += [("property", [s]) for s in item.statements if s.role == "property"]
        for role, statements in groups:
            record = _record(item, statements, role)
            text = "\n".join(
                [record["title"], record["name"], record["description"], *record["relevance"]]
            )
            findings = scan.scan_text("questions", record["source_path"], text)
            if any(f.severity == scan.BLOCK for f in findings):
                stats["dropped_secret"] += 1
                continue
            stats["scan_warnings"] += sum(f.severity == scan.WARN for f in findings)
            records.append(record)
    return records, stats


def summarize(extraction: Extraction, records: Sequence[dict], stats: Counter) -> dict:
    by_role = Counter(r["kind"] for r in records)
    return {
        "files": extraction.files,
        "duplicate_files": extraction.duplicate_files,
        "duplicate_items": extraction.duplicate_items,
        "unparsable_files": len(extraction.unparsable),
        "items": dict(Counter(i.kind for i in extraction.items)),
        "candidates": len(records),
        "by_role": dict(by_role),
        "parse_cleanly": sum(r["parses"] for r in records),
        "with_unknown_inspectors": sum(bool(r["unknown_inspectors"]) for r in records),
        "with_description": sum(bool(r["description"]) for r in records),
        "dropped_secret": stats["dropped_secret"],
        "scan_warnings": stats["scan_warnings"],
    }


def format_report(summary: dict) -> str:
    items = ", ".join(f"{count} {kind}" for kind, count in sorted(summary["items"].items()))
    copies, bad = summary["duplicate_files"], summary["unparsable_files"]
    dropped, warnings = summary["dropped_secret"], summary["scan_warnings"]
    lines = [
        f"files read: {summary['files']} ({copies} identical copies skipped, {bad} not parseable)",
        f"items: {items or 'none'} ({summary['duplicate_items']} duplicate items skipped)",
        f"candidates: {summary['candidates']}",
    ]
    lines += [f"  {role}: {count}" for role, count in sorted(summary["by_role"].items())]
    lines += [
        f"  parse cleanly with the analyzer: {summary['parse_cleanly']}",
        f"  mention an inspector the analyzer lacks: {summary['with_unknown_inspectors']}",
        f"  have a description: {summary['with_description']}",
        f"left out for a secret: {dropped}; scan warnings kept: {warnings}",
    ]
    return "\n".join(lines) + "\n"


def write_candidates(records: Sequence[dict], out: Path) -> None:
    """Write JSON Lines: ASCII-only and key-sorted, so one source always gives the same bytes."""
    lines = [json.dumps(r, sort_keys=True, ensure_ascii=True) for r in records]
    fetch.atomic_write(out, ("\n".join(lines) + "\n").encode("ascii") if lines else b"")


QUESTIONS_NAME = "questions.jsonl"
STATUSES = ("draft", "approved", "rejected")
QUESTION_KINDS = ("applicability", "property", "free")
DIFFICULTIES = ("", "easy", "medium", "hard")
DIALECTS = ("client", "session")
# the fields a question record may hold; anything else is a mistake worth reporting
QUESTION_FIELDS = (
    "id",
    "question",
    "kind",
    "dialect",
    "origin",
    "status",
    "difficulty",
    "candidate_id",
    "reference",
    "notes",
)


def load_questions(path: Path) -> list[dict]:
    """Read a JSON Lines file. A missing file is an empty list; a bad line raises ValueError."""
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as err:
            raise ValueError(f"{path}:{number}: not valid JSON ({err.msg})") from err
        if not isinstance(record, dict):
            # ValueError, not TypeError: callers report every bad line the same way
            raise ValueError(f"{path}:{number}: expected a JSON object")  # noqa: TRY004
        records.append(record)
    return records


def write_questions(records: Sequence[dict], path: Path) -> None:
    """Write questions as JSON Lines in the order given: ASCII-only, keys sorted."""
    lines = [json.dumps(r, sort_keys=True, ensure_ascii=True) for r in records]
    fetch.atomic_write(path, ("\n".join(lines) + "\n").encode("ascii") if lines else b"")


def validate_questions(records: Sequence[dict], candidate_ids: set[str] | None = None) -> list[str]:
    """Problems with a questions file, one message each; empty when it is sound."""
    problems: list[str] = []
    seen: set[str] = set()
    for position, record in enumerate(records, start=1):
        label = f"question {position} ({record.get('id', '?')})"
        problems += [
            f"{label}: unknown field {key!r}" for key in record if key not in QUESTION_FIELDS
        ]
        identifier = record.get("id")
        if not isinstance(identifier, str) or not identifier:
            problems.append(f"{label}: missing id")
        elif identifier in seen:
            problems.append(f"{label}: duplicate id")
        else:
            seen.add(identifier)
        text = record.get("question")
        if not isinstance(text, str) or not text.strip():
            problems.append(f"{label}: empty question")
        choices = (
            ("kind", QUESTION_KINDS, None),
            ("dialect", DIALECTS, None),
            ("status", STATUSES, None),
            ("difficulty", DIFFICULTIES, ""),
        )
        problems += [
            f"{label}: {key} must be one of {list(allowed)}"
            for key, allowed, default in choices
            if record.get(key, default) not in allowed
        ]
        reference = record.get("reference", [])
        if not isinstance(reference, list) or not all(isinstance(r, str) for r in reference):
            problems.append(f"{label}: reference must be a list of strings")
        link = record.get("candidate_id", "")
        if candidate_ids is not None and link and link not in candidate_ids:
            problems.append(f"{label}: candidate_id {link!r} is not in the candidates file")
        if not link and not reference:
            problems.append(f"{label}: needs a candidate_id or an inline reference")
    return problems


def summarize_questions(records: Sequence[dict]) -> dict:
    """Counts only, never question text, so a summary is safe to paste anywhere."""
    return {
        "total": len(records),
        "by_status": dict(Counter(r.get("status", "?") for r in records)),
        "by_kind": dict(Counter(r.get("kind", "?") for r in records)),
        "by_difficulty": dict(Counter(r.get("difficulty") or "unrated" for r in records)),
        "by_origin": dict(Counter(r.get("origin", "?") for r in records)),
    }


def format_questions_summary(summary: dict, problems: Sequence[str]) -> str:
    lines = [f"questions: {summary['total']}"]
    for title, key in (
        ("status", "by_status"),
        ("kind", "by_kind"),
        ("difficulty", "by_difficulty"),
        ("origin", "by_origin"),
    ):
        counts = ", ".join(f"{name} {count}" for name, count in sorted(summary[key].items()))
        lines.append(f"  by {title}: {counts or 'none'}")
    lines.append(f"problems: {len(problems)}")
    lines += [f"  {problem}" for problem in problems]
    return "\n".join(lines) + "\n"


REVIEW_NAME = "review.md"


def render_review(records: Sequence[dict], candidates_by_id: dict[str, dict]) -> str:
    """A readable sheet for reviewing questions: each one beside the relevance it came from.

    The reference is what makes a question checkable: a question that does not match
    its reference would make later scoring unfair. The sheet holds company content,
    so it belongs in the untracked eval directory like the files it is built from.
    """
    counts = Counter(r.get("status", "?") for r in records)
    lines = [
        "# Question review",
        "",
        "Untracked company content: do not commit or share. Check each question describes",
        "what its reference relevance does, and say what to change.",
        "",
        f"{len(records)} questions: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())),
    ]
    for position, record in enumerate(records, start=1):
        candidate = candidates_by_id.get(record.get("candidate_id", ""), {})
        reference = candidate.get("relevance") or record.get("reference") or []
        source = candidate.get("title", "")
        if candidate.get("name"):
            source += f" / {candidate['name']}"
        difficulty = record.get("difficulty") or "unrated"
        heading = f"## {position}. {record.get('id')} ({record.get('kind')}, {difficulty}, "
        lines += [
            "",
            heading + f"{record.get('status')})",
            "",
            f"**Question:** {record.get('question', '')}",
        ]
        if record.get("notes"):
            lines.append(f"\n**Notes:** {record['notes']}")
        if source:
            lines.append(f"\n**Comes from:** {source}")
        lines += [
            "",
            "**Reference relevance:**",
            "",
            "```",
            *(reference or ["(none found)"]),
            "```",
        ]
    return "\n".join(lines) + "\n"


def update_question(records: list[dict], identifier: str, **changes: str | None) -> dict:
    """Change fields of one question in place (None leaves a field alone); KeyError if absent."""
    for record in records:
        if record.get("id") == identifier:
            record.update({key: value for key, value in changes.items() if value is not None})
            return record
    raise KeyError(identifier)


def new_manual_question(
    identifier: str,
    question: str,
    *,
    kind: str,
    dialect: str,
    reference: Sequence[str],
    difficulty: str = "",
    notes: str = "",
) -> dict:
    """A question a person wrote themselves: approved by definition, with its own reference."""
    return {
        "id": identifier,
        "question": question,
        "kind": kind,
        "dialect": dialect,
        "origin": "manual",
        "status": "approved",
        "difficulty": difficulty,
        "candidate_id": "",
        "reference": list(reference),
        "notes": notes,
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    base = fetch.corpus_base() / "eval" / "questions"
    parser = argparse.ArgumentParser(
        prog="python -m tools.eval.questions",
        description="Collect and manage questions for evaluating relevance help.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    extract_parser = commands.add_parser("extract", help="read .bes files and write candidates")
    extract_parser.add_argument(
        "--source", type=Path, required=True, help="directory of .bes files"
    )
    extract_parser.add_argument("--out", type=Path, default=base / CANDIDATES_NAME)

    managers = {
        "summary": commands.add_parser("summary", help="count the questions and check the file"),
        "update": commands.add_parser("update", help="change a question's status or wording"),
        "add": commands.add_parser("add", help="add a question you wrote, with its reference"),
        "review": commands.add_parser("review", help="write a readable sheet to review them"),
    }
    for sub in managers.values():
        sub.add_argument("--questions", type=Path, default=base / QUESTIONS_NAME)
        sub.add_argument("--candidates", type=Path, default=base / CANDIDATES_NAME)
    managers["update"].add_argument("--id", required=True, dest="identifier")
    managers["update"].add_argument("--status", choices=STATUSES)
    managers["update"].add_argument("--question")
    managers["update"].add_argument("--difficulty", choices=DIFFICULTIES[1:])
    managers["update"].add_argument("--notes")
    managers["review"].add_argument("--out", type=Path, default=base / REVIEW_NAME)
    managers["review"].add_argument(
        "--status", choices=STATUSES, help="only questions with this status"
    )
    adder = managers["add"]
    adder.add_argument("--question", required=True)
    adder.add_argument("--kind", choices=QUESTION_KINDS, default="free")
    adder.add_argument("--dialect", choices=DIALECTS, default="client")
    adder.add_argument("--reference", action="append", default=[], help="repeat per relevance line")
    adder.add_argument("--difficulty", choices=DIFFICULTIES[1:], default="")
    adder.add_argument("--notes", default="")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def _candidate_ids(path: Path) -> set[str] | None:
    """The ids in a candidates file, or None when there is no such file to check against."""
    if not path.exists():
        return None
    return {record["id"] for record in load_questions(path)}


def _run_questions_command(args: argparse.Namespace) -> int:
    try:
        records = load_questions(args.questions)
        known = _candidate_ids(args.candidates)
    except (OSError, ValueError, KeyError) as err:
        # a malformed file is a user error; say which line, not a traceback
        log.error("cannot read: %s", err)  # noqa: TRY400
        return 2
    if args.command == "summary":
        problems = validate_questions(records, known)
        sys.stdout.write(format_questions_summary(summarize_questions(records), problems))
        return 1 if problems else 0
    if args.command == "review":
        shown = [r for r in records if not args.status or r.get("status") == args.status]
        by_id = {c["id"]: c for c in load_questions(args.candidates)} if known is not None else {}
        fetch.atomic_write(args.out, render_review(shown, by_id).encode("utf-8"))
        sys.stdout.write(
            f"wrote {len(shown)} questions to {args.out} (untracked company content)\n"
        )
        return 0
    if args.command == "update":
        try:
            update_question(
                records,
                args.identifier,
                status=args.status,
                question=args.question,
                difficulty=args.difficulty,
                notes=args.notes,
            )
        except KeyError:
            # a wrong id is a user error; a traceback would only bury the message
            log.error("no question with id %s", args.identifier)  # noqa: TRY400
            return 2
        sys.stdout.write(f"updated {args.identifier}\n")
    else:  # add
        manual = [r for r in records if str(r.get("id", "")).startswith("manual-")]
        identifier = f"manual-{len(manual) + 1:03d}"
        records.append(
            new_manual_question(
                identifier,
                args.question,
                kind=args.kind,
                dialect=args.dialect,
                reference=args.reference,
                difficulty=args.difficulty,
                notes=args.notes,
            )
        )
        sys.stdout.write(f"added {identifier}\n")
    problems = validate_questions(records, known)
    if problems:
        sys.stdout.write("not saved: " + "; ".join(problems) + "\n")
        return 2
    write_questions(records, args.questions)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if args.command != "extract":
        return _run_questions_command(args)
    if not args.source.is_dir():
        log.error("--source %s is not a directory", args.source)
        return 2
    extraction = extract(args.source)
    records, stats = candidates(extraction.items)
    write_candidates(records, args.out)
    sys.stdout.write(format_report(summarize(extraction, records, stats)))
    sys.stdout.write(
        f"written to {args.out} (untracked; company content, not for the repository)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

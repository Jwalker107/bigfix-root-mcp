"""Sanity measurements of search over the knowledge index.

These check the *mechanics* of retrieval, not whether answers satisfy a person:

  self-retrieval   Search for an example by its own relevance text. Every example
                   should come back first; a miss means tokenisation, quoting or
                   ranking is mishandling relevance syntax.
  heading lookup   Search for a documentation chunk by its own heading. A miss
                   means the heading is ambiguous or buried (for example, the
                   same page exists twice).
  crowding         How many chunk texts appear on more than one page, and how
                   often a top-5 result list contains a duplicate. Near-identical
                   pages push other answers out of a small result list.

Each query is derived from the index itself, so a high score is expected and is
not evidence of relevance: a person's question rarely repeats the indexed text.
Judging relevance needs human-written questions (the with/without-retrieval
comparison, not this tool). What this does catch are mechanical faults, and it
quantifies the duplicate-page problem.

It searches through bigfix_root_mcp.knowledge, so it exercises the same code and
the same read-only index access the server uses. Nothing leaves the machine.

    python -m tools.eval.retrieval [--index PATH]
"""

import argparse
import logging
import os
import sqlite3
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from bigfix_root_mcp import knowledge
from tools.ingest import fetch

log = logging.getLogger("corpus.eval")

INDEX_NAME = "relevance.sqlite"
TOP_K = 5
SAMPLE_FAILURES = 8
CROWDING_EXAMPLES = 8


@dataclass(frozen=True)
class ExampleKey:
    query: str
    path: str
    form: str


@dataclass(frozen=True)
class ChunkKey:
    path: str
    heading: str
    kind: str
    text: str


def load(index_path: Path) -> tuple[list[ExampleKey], list[ChunkKey]]:
    """Read the examples and chunks to query for (read-only)."""
    connection = sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        examples = [
            ExampleKey(query=r[0], path=r[1], form=r[2])
            for r in connection.execute("SELECT query, path, form FROM examples ORDER BY id")
        ]
        chunks = [
            ChunkKey(path=r[0], heading=r[1], kind=r[2], text=r[3])
            for r in connection.execute("SELECT path, heading, kind, text FROM chunks ORDER BY id")
        ]
    finally:
        connection.close()
    return examples, chunks


def _rank(found: list, matches) -> int | None:
    """1-based position of the first result for which `matches` is true."""
    for position, item in enumerate(found, start=1):
        if matches(item):
            return position
    return None


def _query_text(text: str) -> str:
    """The first MAX_QUERY_CHARS characters, the most the search accepts."""
    return text[: knowledge.MAX_QUERY_CHARS]


def self_retrieval(examples: Sequence[ExampleKey]) -> dict:
    """Search for each example by its own text; rank of the same query among results."""
    ranks: list[int | None] = []
    skipped = 0
    misses: list[tuple[str, str]] = []
    for example in examples:
        text = _query_text(example.query)
        if not knowledge.parse_query(text):
            skipped += 1  # nothing searchable in it (all punctuation)
            continue
        found = knowledge.search(text, kind="example", limit=TOP_K)["examples"]
        rank = _rank(found, lambda e, q=example.query: e["query"] == q)
        ranks.append(rank)
        if rank is None:
            misses.append((example.path, example.query[:90]))
    return {"total": len(ranks), "skipped": skipped, **_summary(ranks), "misses": misses}


def _summary(ranks: list[int | None]) -> dict:
    total = len(ranks)
    first = sum(1 for r in ranks if r == 1)
    within = sum(1 for r in ranks if r is not None)
    return {
        "at_1": first,
        "at_k": within,
        "not_found": total - within,
        "rate_at_1": first / total if total else 0.0,
        "rate_at_k": within / total if total else 0.0,
    }


def heading_lookup(chunks: Sequence[ChunkKey]) -> dict:
    """Search for each chunk by its heading; rank of its (path, heading) among results."""
    ranks: list[int | None] = []
    failures: list[tuple[str, str, str]] = []
    crowded = 0
    seen: set[tuple[str, str]] = set()
    for chunk in chunks:
        key = (chunk.path, chunk.heading)
        if key in seen:
            continue  # a long section is several chunks with one heading: ask once
        seen.add(key)
        text = _query_text(chunk.heading)
        if not knowledge.parse_query(text):
            continue
        found = knowledge.search(text, kind=chunk.kind, limit=TOP_K)["chunks"]
        rank = _rank(found, lambda c, k=key: (c["path"], c["heading"]) == k)
        ranks.append(rank)
        texts = [c["text"] for c in found]
        crowded += len(set(texts)) < len(texts)
        if rank != 1:
            cause = "not in the top results" if rank is None else f"ranked {rank}"
            failures.append((chunk.path, chunk.heading[:70], cause))
    out = _summary(ranks)
    out.update({"total": len(ranks), "crowded_result_lists": crowded, "failures": failures})
    return out


def duplicate_chunks(chunks: Sequence[ChunkKey]) -> dict:
    """Chunk texts that occur on more than one page, and which pages share them."""
    pages_by_text: dict[str, set[str]] = {}
    for chunk in chunks:
        pages_by_text.setdefault(chunk.text, set()).add(chunk.path)
    shared = {text: pages for text, pages in pages_by_text.items() if len(pages) > 1}
    pairs: Counter = Counter()
    for pages in shared.values():
        pairs[tuple(sorted(pages))] += 1
    redundant = sum(len(pages) - 1 for pages in shared.values())
    return {
        "distinct_texts": len(pages_by_text),
        "texts_on_several_pages": len(shared),
        "redundant_chunks": redundant,
        "page_groups": pairs.most_common(CROWDING_EXAMPLES),
    }


def _pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def _rate_line(stats: dict) -> str:
    first = f"first result: {stats['at_1']} ({_pct(stats['rate_at_1'])})"
    within = f"in top {TOP_K}: {stats['at_k']} ({_pct(stats['rate_at_k'])})"
    return f"  {first}   {within}   not found: {stats['not_found']}"


def format_report(results: dict) -> str:
    s, h, d = results["self_retrieval"], results["heading_lookup"], results["duplicates"]
    crowded = h["crowded_result_lists"]
    crowded_share = _pct(crowded / h["total"]) if h["total"] else "n/a"
    lines = [
        "These are mechanics checks: each query is derived from the index itself, so high",
        "scores are expected and are not evidence that real questions are answered.",
        "",
        (
            f"self-retrieval: search an example by its own text ({s['total']} examples,"
            f" {s['skipped']} had nothing searchable)"
        ),
        _rate_line(s),
    ]
    lines += [f"    miss: {path}: {query!r}" for path, query in s["misses"][:SAMPLE_FAILURES]]
    lines += [
        "",
        f"heading lookup: search a chunk by its heading ({h['total']} distinct headings)",
        _rate_line(h),
        f"  result lists holding a duplicate chunk text: {crowded} ({crowded_share})",
    ]
    lines += [
        f"    {path}: {head!r} -> {cause}" for path, head, cause in h["failures"][:SAMPLE_FAILURES]
    ]
    lines += [
        "",
        (
            f"duplicate pages: {d['distinct_texts']} distinct chunk texts;"
            f" {d['texts_on_several_pages']} appear on more than one page"
            f" ({d['redundant_chunks']} redundant chunks)"
        ),
    ]
    lines += [
        f"    {count:4} shared chunks: {' = '.join(pages)}" for pages, count in d["page_groups"]
    ]
    return "\n".join(lines) + "\n"


def measure(index_path: Path) -> dict:
    """Run every measurement against the index at `index_path`."""
    examples, chunks = load(index_path)
    previous = os.environ.get(knowledge.INDEX_ENV)
    os.environ[knowledge.INDEX_ENV] = str(index_path)
    try:
        results = {
            "self_retrieval": self_retrieval(examples),
            "heading_lookup": heading_lookup(chunks),
            "duplicates": duplicate_chunks(chunks),
        }
    finally:
        if previous is None:
            os.environ.pop(knowledge.INDEX_ENV, None)
        else:
            os.environ[knowledge.INDEX_ENV] = previous
    return results


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.eval.retrieval",
        description="Sanity measurements of search over the knowledge index.",
    )
    parser.add_argument(
        "--index", type=Path, default=fetch.corpus_base() / "index" / INDEX_NAME, help="index file"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    try:
        results = measure(args.index)
    except (OSError, sqlite3.Error) as err:
        # a missing or unreadable index is a user error; a traceback would only bury the message
        log.error("cannot read the index %s: %s", args.index, err)  # noqa: TRY400
        return 2
    sys.stdout.write(format_report(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Write the relevance knowledge index (SQLite with FTS5).

Schema version 2:

  meta      key/value: schema_version, built_at (UTC), sqlite_version, counts,
            and analyzer_version when the examples were analysed.
  sources   one row per ingested source: where it came from and exactly which
            revision (a git commit, or the SHA-256 of the fetched file).
  chunks    heading-sized pieces of documentation. kind is 'explanation' or
            'inspector' (the generated per-inspector reference).
  examples  relevance examples, each tied to the chunk it appears in. answers
            and errors are JSON arrays of strings. `incomplete` = 1 means the
            expected answers may be partial, so do not score them exactly.
            The offline analyzer's verdict is stored with each example:
              parses              1 / 0, or NULL when it was not analysed. 0 on
                                  an example whose `errors` are documented can
                                  be expected; otherwise the syntax is suspect.
              parse_error         why it did not parse ('' when it did)
              unknown_inspectors  JSON array of names the analyzer's snapshot
                                  lacks (newer documentation, or a typo)
            `dialect` is the example's own. dialect_source says where it came
            from: 'source' (the documentation states it, or it inherits the
            chunk's) or 'analyzer' (the source did not say, and the analyzer was
            definite and consistent).
  chunks_fts, examples_fts
            external-content FTS5 tables (porter stemming over unicode61).
            chunks_fts indexes heading, section_path and text; examples_fts
            indexes the query and the context around it.

dialect is one of session, client, both, unknown. `unknown` means nothing says
(not that it applies to neither); a reader filtering by dialect should treat it
as matching both.

The file is built beside its destination and renamed into place, so a failed
build never leaves a half-written index behind. Rows are inserted in the order
given, which keeps ids stable for identical input.
"""

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 2

DIALECTS = ("session", "client", "both", "unknown")
DIALECT_SOURCES = ("source", "analyzer")
CHUNK_KINDS = ("explanation", "inspector")

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('git', 'url')),
    origin TEXT NOT NULL,
    revision TEXT NOT NULL,
    license TEXT NOT NULL,
    version TEXT NOT NULL,
    dialect TEXT NOT NULL
);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    path TEXT NOT NULL,
    heading TEXT NOT NULL,
    section_path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('explanation', 'inspector')),
    dialect TEXT NOT NULL CHECK (dialect IN ('session', 'client', 'both', 'unknown')),
    version TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE examples (
    id INTEGER PRIMARY KEY,
    chunk_id INTEGER NOT NULL REFERENCES chunks(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    path TEXT NOT NULL,
    section_path TEXT NOT NULL,
    dialect TEXT NOT NULL CHECK (dialect IN ('session', 'client', 'both', 'unknown')),
    form TEXT NOT NULL,
    query TEXT NOT NULL,
    answers TEXT NOT NULL,
    errors TEXT NOT NULL,
    result_type TEXT NOT NULL,
    incomplete INTEGER NOT NULL CHECK (incomplete IN (0, 1)),
    context TEXT NOT NULL,
    dialect_source TEXT NOT NULL CHECK (dialect_source IN ('source', 'analyzer')),
    parses INTEGER CHECK (parses IN (0, 1)),
    parse_error TEXT NOT NULL,
    unknown_inspectors TEXT NOT NULL
);
CREATE INDEX chunks_by_source ON chunks(source_id);
CREATE INDEX examples_by_chunk ON examples(chunk_id);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    heading, section_path, text,
    content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE examples_fts USING fts5(
    query, context,
    content='examples', content_rowid='id', tokenize='porter unicode61'
);
"""


@dataclass
class SourceRow:
    id: str
    kind: str
    origin: str
    revision: str
    license: str = ""
    version: str = ""
    dialect: str = "unknown"


@dataclass
class ChunkRow:
    source_id: str
    path: str
    heading: str
    section_path: tuple[str, ...]
    kind: str
    dialect: str
    version: str
    text: str
    examples: list["ExampleRow"] = field(default_factory=list)


@dataclass
class ExampleRow:
    form: str
    query: str
    answers: tuple[str, ...]
    errors: tuple[str, ...]
    result_type: str
    incomplete: bool
    context: str
    # the example's own dialect; None inherits the chunk's
    dialect: str | None = None
    dialect_source: str = "source"
    # the analyzer's verdict; None means it was not analysed
    parses: bool | None = None
    parse_error: str = ""
    unknown_inspectors: tuple[str, ...] = ()


@dataclass
class IndexData:
    sources: list[SourceRow] = field(default_factory=list)
    chunks: list[ChunkRow] = field(default_factory=list)
    # recorded in meta so an index says which analyzer judged its examples
    analyzer_version: str = ""

    @property
    def example_count(self) -> int:
        return sum(len(chunk.examples) for chunk in self.chunks)


def _section_text(section_path: tuple[str, ...]) -> str:
    return " > ".join(section_path)


def write_index(path: Path, data: IndexData, *, built_at: datetime | None = None) -> None:
    """Build the index at `path` atomically. Raises on any inconsistency in `data`."""
    source_ids = {source.id for source in data.sources}
    if len(source_ids) != len(data.sources):
        raise ValueError("duplicate source id in index data")
    for chunk in data.chunks:
        if chunk.source_id not in source_ids:
            raise ValueError(f"chunk refers to unknown source {chunk.source_id!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    connection = sqlite3.connect(tmp)
    try:
        connection.executescript(_SCHEMA)
        connection.executemany(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (s.id, s.kind, s.origin, s.revision, s.license, s.version, s.dialect)
                for s in data.sources
            ],
        )
        for chunk in data.chunks:
            cursor = connection.execute(
                "INSERT INTO chunks (source_id, path, heading, section_path, kind, dialect,"
                " version, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk.source_id,
                    chunk.path,
                    chunk.heading,
                    _section_text(chunk.section_path),
                    chunk.kind,
                    chunk.dialect,
                    chunk.version,
                    chunk.text,
                ),
            )
            chunk_id = cursor.lastrowid
            connection.executemany(
                "INSERT INTO examples (chunk_id, source_id, path, section_path, dialect, form,"
                " query, answers, errors, result_type, incomplete, context, dialect_source,"
                " parses, parse_error, unknown_inspectors)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        chunk_id,
                        chunk.source_id,
                        chunk.path,
                        _section_text(chunk.section_path),
                        example.dialect or chunk.dialect,
                        example.form,
                        example.query,
                        json.dumps(example.answers),
                        json.dumps(example.errors),
                        example.result_type,
                        int(example.incomplete),
                        example.context,
                        example.dialect_source,
                        None if example.parses is None else int(example.parses),
                        example.parse_error,
                        json.dumps(example.unknown_inspectors),
                    )
                    for example in chunk.examples
                ],
            )
        connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
        connection.execute("INSERT INTO examples_fts(examples_fts) VALUES ('rebuild')")
        when = (built_at or datetime.now(UTC)).replace(microsecond=0).isoformat()
        meta = [
            ("schema_version", str(SCHEMA_VERSION)),
            ("built_at", when),
            ("sqlite_version", sqlite3.sqlite_version),
            ("source_count", str(len(data.sources))),
            ("chunk_count", str(len(data.chunks))),
            ("example_count", str(data.example_count)),
        ]
        if data.analyzer_version:
            meta.append(("analyzer_version", data.analyzer_version))
        connection.executemany("INSERT INTO meta VALUES (?, ?)", meta)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.close()
        tmp.unlink(missing_ok=True)
        raise
    connection.close()
    os.replace(tmp, path)

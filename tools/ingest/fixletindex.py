"""Write the reference fixlet index (SQLite with FTS5).

This is company content, so it is kept apart from the documentation index
(relevance.sqlite): a separate file, never tracked, never packaged, never meant to be
distributed. The file says so in its own meta table.

Schema version 2:

  meta     key/value: schema_version, built_at (UTC), sqlite_version, counts,
           analyzer_version, and `confidential`.
  reads    one row per source read: its kind (dir or git), location, the commit and
           commit date (git), the UTC time it was read, a fingerprint of the files, and
           counts. This is how a result is traced to one specific read. `source_group`
           says which sources are counted together; one without a group is its own.
  clauses  one row per piece of relevance worth finding:
             applicability  a Fixlet or Task: all its relevance lines together, as a
                            JSON array in `relevance` (exact lines), and joined by
                            newlines in `relevance_text`, which is what the full-text
                            index reads.
             property      an Analysis property, one row each, with its name.
           Each carries the purpose text (title, file name, description, an excerpt of
           the action script), the analyzer's verdict (parses, parse_error,
           unknown_inspectors) and `excluded`: a JSON array naming any purpose field
           that was left out because the secret scan found a credential in it.
           `content_id` is the same id the question collector gives an item, so a
           question's own source can be held out of an evaluation. `platform` is
           windows, mac, unix, multi or none, from the item's own operating-system test.
  conventions
           counted habits per (source_group, role, platform): a line, a top-level
           `and` part of a line (conjunct), an opening line or a line's shape, with
           how many distinct items hold it, how many there were, the share and a few
           example content ids. `platform` 'all' rolls a group's platforms together.
           Groups are never pooled. See fixletconventions.py.
  clauses_fts
           external-content FTS5 over title, file_name, description, action_excerpt,
           property_name and relevance_text (porter stemming over unicode61).

dialect is always `client` today: BES content relevance is evaluated on the client.

The file is built beside its destination and renamed into place, so a failed build
never leaves a half-written index. Rows are inserted in the order given. The index is
rebuilt, not migrated, when the schema changes.
"""

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 2

ROLES = ("applicability", "property")
DIALECTS = ("session", "client", "both", "unknown")
CONFIDENTIAL_NOTE = "company reference content: do not distribute or commit"

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE reads (
    source_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('dir', 'git')),
    source_group TEXT NOT NULL,
    location TEXT NOT NULL,
    branch TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    commit_date TEXT NOT NULL,
    subpath TEXT NOT NULL,
    read_at TEXT NOT NULL,
    tree_sha256 TEXT NOT NULL,
    files INTEGER NOT NULL,
    duplicate_files INTEGER NOT NULL,
    unparsable_files INTEGER NOT NULL,
    items INTEGER NOT NULL
);
CREATE TABLE clauses (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES reads(source_id),
    path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    content_id TEXT NOT NULL,
    item_kind TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('applicability', 'property')),
    platform TEXT NOT NULL,
    property_name TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    action_excerpt TEXT NOT NULL,
    relevance TEXT NOT NULL,
    relevance_text TEXT NOT NULL,
    dialect TEXT NOT NULL CHECK (dialect IN ('session', 'client', 'both', 'unknown')),
    parses INTEGER CHECK (parses IN (0, 1)),
    parse_error TEXT NOT NULL,
    unknown_inspectors TEXT NOT NULL,
    excluded TEXT NOT NULL
);
CREATE TABLE conventions (
    id INTEGER PRIMARY KEY,
    source_group TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('applicability', 'property')),
    platform TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('line', 'conjunct', 'opening', 'shape')),
    pattern TEXT NOT NULL,
    files INTEGER NOT NULL,
    total INTEGER NOT NULL,
    share REAL NOT NULL,
    examples TEXT NOT NULL
);
CREATE INDEX clauses_by_source ON clauses(source_id);
CREATE INDEX clauses_by_platform ON clauses(role, platform);
CREATE INDEX conventions_by_cell ON conventions(source_group, role, platform);
CREATE INDEX clauses_by_content ON clauses(content_id);
CREATE VIRTUAL TABLE clauses_fts USING fts5(
    title, file_name, description, action_excerpt, property_name, relevance_text,
    content='clauses', content_rowid='id', tokenize='porter unicode61'
);
"""


@dataclass
class ReadRow:
    source_id: str
    kind: str
    location: str
    read_at: str
    tree_sha256: str
    source_group: str = ""
    branch: str = ""
    commit_sha: str = ""
    commit_date: str = ""
    subpath: str = ""
    files: int = 0
    duplicate_files: int = 0
    unparsable_files: int = 0
    items: int = 0


@dataclass
class ClauseRow:
    source_id: str
    path: str
    file_name: str
    content_id: str
    item_kind: str
    role: str
    property_name: str
    title: str
    description: str
    action_excerpt: str
    relevance: tuple[str, ...]
    platform: str = "none"
    dialect: str = "client"
    # the analyzer's verdict; None means it was not analysed
    parses: bool | None = None
    parse_error: str = ""
    unknown_inspectors: tuple[str, ...] = ()
    # purpose fields left out because the secret scan found a credential in them
    excluded: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConventionRow:
    source_group: str
    role: str
    platform: str
    kind: str
    pattern: str
    files: int
    total: int
    share: float
    examples: tuple[str, ...] = ()


@dataclass
class FixletIndexData:
    reads: list[ReadRow] = field(default_factory=list)
    clauses: list[ClauseRow] = field(default_factory=list)
    conventions: list[ConventionRow] = field(default_factory=list)
    analyzer_version: str = ""
    # the support thresholds the conventions were counted with, recorded in meta
    min_files: int = 0
    min_share: dict[str, float] = field(default_factory=dict)


def write_index(path: Path, data: FixletIndexData, *, built_at: datetime | None = None) -> None:
    """Build the index at `path` atomically. Raises on any inconsistency in `data`."""
    source_ids = {read.source_id for read in data.reads}
    if len(source_ids) != len(data.reads):
        raise ValueError("duplicate source id in fixlet index data")
    for clause in data.clauses:
        if clause.source_id not in source_ids:
            raise ValueError(f"clause refers to unknown source {clause.source_id!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    connection = sqlite3.connect(tmp)
    try:
        connection.executescript(_SCHEMA)
        connection.executemany(
            "INSERT INTO reads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r.source_id,
                    r.kind,
                    r.source_group,
                    r.location,
                    r.branch,
                    r.commit_sha,
                    r.commit_date,
                    r.subpath,
                    r.read_at,
                    r.tree_sha256,
                    r.files,
                    r.duplicate_files,
                    r.unparsable_files,
                    r.items,
                )
                for r in data.reads
            ],
        )
        connection.executemany(
            "INSERT INTO clauses (source_id, path, file_name, content_id, item_kind, role,"
            " platform, property_name, title, description, action_excerpt, relevance,"
            " relevance_text, dialect, parses, parse_error, unknown_inspectors, excluded)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    c.source_id,
                    c.path,
                    c.file_name,
                    c.content_id,
                    c.item_kind,
                    c.role,
                    c.platform,
                    c.property_name,
                    c.title,
                    c.description,
                    c.action_excerpt,
                    json.dumps(c.relevance),
                    "\n".join(c.relevance),
                    c.dialect,
                    None if c.parses is None else int(c.parses),
                    c.parse_error,
                    json.dumps(c.unknown_inspectors),
                    json.dumps(c.excluded),
                )
                for c in data.clauses
            ],
        )
        connection.execute("INSERT INTO clauses_fts(clauses_fts) VALUES ('rebuild')")
        connection.executemany(
            "INSERT INTO conventions (source_group, role, platform, kind, pattern, files,"
            " total, share, examples) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    v.source_group,
                    v.role,
                    v.platform,
                    v.kind,
                    v.pattern,
                    v.files,
                    v.total,
                    v.share,
                    json.dumps(v.examples),
                )
                for v in data.conventions
            ],
        )
        when = (built_at or datetime.now(UTC)).replace(microsecond=0).isoformat()
        meta = [
            ("schema_version", str(SCHEMA_VERSION)),
            ("built_at", when),
            ("sqlite_version", sqlite3.sqlite_version),
            ("read_count", str(len(data.reads))),
            ("clause_count", str(len(data.clauses))),
            ("convention_count", str(len(data.conventions))),
            ("convention_min_files", str(data.min_files)),
            ("convention_min_share", json.dumps(data.min_share, sort_keys=True)),
            ("confidential", CONFIDENTIAL_NOTE),
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

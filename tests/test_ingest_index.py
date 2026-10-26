"""tools/ingest/index.py: SQLite + FTS5 index writer."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from tools.ingest import index

BUILT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def sample_data():
    data = index.IndexData(
        sources=[
            index.SourceRow(
                "s1", "git", "https://example.com/r.git", "a" * 40, "Apache-2.0", "", "both"
            ),
            index.SourceRow(
                "s2", "url", "https://example.com/g.pdf", "b" * 64, "", "8.1", "client"
            ),
        ]
    )
    data.chunks = [
        index.ChunkRow(
            "s1",
            "guide/computers.md",
            "Computers",
            ("guide", "Computers"),
            "explanation",
            "client",
            "",
            "The bes computers inspector lists computers visible to the console.",
            examples=[
                index.ExampleRow(
                    "qna",
                    "names of bes computers",
                    ("a", "b"),
                    (),
                    "plural string",
                    False,
                    "guide > Computers Lists every computer.",
                )
            ],
        ),
        index.ChunkRow(
            "s1",
            "_reference/docs/bit-set.md",
            "bit <integer> of <bit set> : boolean",
            ("bit-set", "bit <integer> of <bit set> : boolean"),
            "inspector",
            "unknown",
            "",
            "Returns the value of the bit at the nth position.",
        ),
        index.ChunkRow(
            "s2",
            "s2.md",
            "Page 1",
            ("s2", "Page 1"),
            "explanation",
            "client",
            "8.1",
            "Registry keys.",
        ),
    ]
    return data


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "index" / "relevance.sqlite"
    index.write_index(path, sample_data(), built_at=BUILT)
    connection = sqlite3.connect(path)
    yield connection
    connection.close()


def test_meta_and_schema_version(db):
    meta = dict(db.execute("SELECT key, value FROM meta"))
    assert meta["schema_version"] == str(index.SCHEMA_VERSION)
    assert meta["built_at"] == "2026-01-02T03:04:05+00:00"
    assert (meta["source_count"], meta["chunk_count"], meta["example_count"]) == ("2", "3", "1")
    assert db.execute("PRAGMA user_version").fetchone()[0] == index.SCHEMA_VERSION


def test_rows_round_trip(db):
    sources = db.execute("SELECT id, kind, revision, dialect FROM sources ORDER BY id").fetchall()
    assert sources == [("s1", "git", "a" * 40, "both"), ("s2", "url", "b" * 64, "client")]
    path, section = db.execute(
        "SELECT path, section_path FROM chunks WHERE heading = 'Computers'"
    ).fetchone()
    assert (path, section) == ("guide/computers.md", "guide > Computers")
    query, answers, incomplete = db.execute(
        "SELECT query, answers, incomplete FROM examples"
    ).fetchone()
    assert (query, json.loads(answers), incomplete) == ("names of bes computers", ["a", "b"], 0)


def test_chunk_search_uses_stemming(db):
    # the text says "computers"; the query says "computer"
    hits = db.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'computer'").fetchall()
    assert len(hits) == 1
    heading = db.execute("SELECT heading FROM chunks WHERE id = ?", hits[0]).fetchone()[0]
    assert heading == "Computers"


def test_chunk_search_covers_heading_and_section_path(db):
    by_heading = db.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'heading:bit'"
    ).fetchone()
    assert by_heading[0] == 1
    by_path = db.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'section_path:guide'"
    ).fetchone()
    assert by_path[0] == 1


def test_example_search_covers_query_and_context(db):
    assert (
        db.execute(
            "SELECT count(*) FROM examples_fts WHERE examples_fts MATCH 'query:computers'"
        ).fetchone()[0]
        == 1
    )
    assert (
        db.execute(
            "SELECT count(*) FROM examples_fts WHERE examples_fts MATCH 'context:every'"
        ).fetchone()[0]
        == 1
    )


def test_examples_point_at_their_chunk(db):
    heading = db.execute(
        "SELECT c.heading FROM examples e JOIN chunks c ON c.id = e.chunk_id"
    ).fetchone()[0]
    assert heading == "Computers"


def test_identical_input_gives_identical_rows(tmp_path):
    dumps = []
    for name in ("one", "two"):
        path = tmp_path / f"{name}.sqlite"
        index.write_index(path, sample_data(), built_at=BUILT)
        connection = sqlite3.connect(path)
        dumps.append(
            [
                connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in ("meta", "sources", "chunks", "examples")
            ]
        )
        connection.close()
    assert dumps[0] == dumps[1]


def test_a_failed_build_leaves_the_existing_index_untouched(tmp_path):
    path = tmp_path / "relevance.sqlite"
    index.write_index(path, sample_data(), built_at=BUILT)
    before = path.read_bytes()
    bad = sample_data()
    bad.chunks[0].dialect = "martian"  # violates the CHECK constraint mid-build
    with pytest.raises(sqlite3.IntegrityError):
        index.write_index(path, bad, built_at=BUILT)
    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".tmp").exists()


def test_rebuilding_replaces_the_index(tmp_path):
    path = tmp_path / "relevance.sqlite"
    index.write_index(path, sample_data(), built_at=BUILT)
    smaller = sample_data()
    smaller.chunks = smaller.chunks[:1]
    index.write_index(path, smaller, built_at=BUILT)
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
    connection.close()


def test_a_chunk_for_an_unknown_source_is_rejected_before_anything_is_written(tmp_path):
    data = sample_data()
    data.chunks[0].source_id = "nope"
    path = tmp_path / "relevance.sqlite"
    with pytest.raises(ValueError, match="unknown source"):
        index.write_index(path, data, built_at=BUILT)
    assert not path.exists()


def test_duplicate_source_ids_are_rejected(tmp_path):
    data = sample_data()
    data.sources.append(index.SourceRow("s1", "git", "x", "y"))
    with pytest.raises(ValueError, match="duplicate"):
        index.write_index(tmp_path / "relevance.sqlite", data)


def verdict_rows(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT query, dialect, dialect_source, parses, parse_error, unknown_inspectors"
            " FROM examples ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def data_with_verdicts():
    data = sample_data()
    data.analyzer_version = "9.9.9"
    data.chunks[0].examples = [
        index.ExampleRow(
            "qna", "q1", ("a",), (), "", False, "c",
            dialect="session", dialect_source="analyzer", parses=True,
            unknown_inspectors=("idp user",),
        ),
        index.ExampleRow("qna", "q2", (), (), "", False, "c", parses=False, parse_error="bad"),
        index.ExampleRow("qna", "q3", (), (), "", False, "c"),
    ]  # fmt: skip
    return data


def test_example_verdicts_round_trip(tmp_path):
    path = tmp_path / "relevance.sqlite"
    data = data_with_verdicts()
    index.write_index(path, data, built_at=BUILT)
    inherited = data.chunks[0].dialect
    assert verdict_rows(path) == [
        ("q1", "session", "analyzer", 1, "", '["idp user"]'),
        ("q2", inherited, "source", 0, "bad", "[]"),
        ("q3", inherited, "source", None, "", "[]"),  # NULL: not analysed
    ]


def test_the_analyzer_version_is_recorded_only_when_there_is_one(tmp_path):
    with_version, without = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
    index.write_index(with_version, data_with_verdicts(), built_at=BUILT)
    index.write_index(without, sample_data(), built_at=BUILT)

    def meta(path):
        connection = sqlite3.connect(path)
        try:
            return dict(connection.execute("SELECT key, value FROM meta"))
        finally:
            connection.close()

    assert meta(with_version)["analyzer_version"] == "9.9.9"
    assert "analyzer_version" not in meta(without)
    assert meta(with_version)["schema_version"] == str(index.SCHEMA_VERSION)


@pytest.mark.parametrize(
    "bad",
    [{"dialect_source": "guess"}, {"dialect": "martian"}],
)
def test_the_schema_refuses_an_unknown_dialect_or_dialect_source(tmp_path, bad):
    data = sample_data()
    data.chunks[0].examples = [index.ExampleRow("qna", "q", (), (), "", False, "c", **bad)]
    path = tmp_path / "relevance.sqlite"
    with pytest.raises(sqlite3.IntegrityError):
        index.write_index(path, data, built_at=BUILT)
    assert not path.exists()

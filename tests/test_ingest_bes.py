"""tools/ingest/bes.py: finding and reading .bes files, shared by the question collector."""

import contextlib
import subprocess
import sys

import pytest

from tools.ingest import bes


def _document(relevance: str = "true") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<Fixlet><Title>T</Title><Relevance>{relevance}</Relevance></Fixlet></BES>"
    ).encode()


def _write(root, relative, data=None):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_document() if data is None else data)
    return path


class TestFindingFiles:
    def test_the_extension_matches_in_any_letter_case(self, tmp_path):
        for name in ("a.bes", "b.BES", "c.Bes"):
            _write(tmp_path, name)
        _write(tmp_path, "d.txt")
        assert [p.name for p in bes.bes_files(tmp_path)] == ["a.bes", "b.BES", "c.Bes"]

    def test_order_is_by_relative_posix_path(self, tmp_path):
        # "a-b/x" sorts before "a/x" as text, which a directory-first walk would not give
        for relative in ("a/x.bes", "a-b/x.bes", "b.bes"):
            _write(tmp_path, relative)
        found = [p.relative_to(tmp_path).as_posix() for p in bes.bes_files(tmp_path)]
        assert found == ["a-b/x.bes", "a/x.bes", "b.bes"]

    def test_tooling_directories_are_skipped(self, tmp_path):
        _write(tmp_path, "keep/one.bes")
        for skipped in (".git", "node_modules", "venv", "__pycache__"):
            _write(tmp_path, f"{skipped}/hidden.bes")
        assert [p.name for p in bes.bes_files(tmp_path)] == ["one.bes"]

    def test_a_missing_directory_gives_nothing(self, tmp_path):
        assert bes.bes_files(tmp_path / "absent") == []

    def test_a_linked_file_is_not_followed(self, tmp_path):
        real = _write(tmp_path / "src", "real.bes")
        link = tmp_path / "src" / "link.bes"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links are not available here")
        assert [p.name for p in bes.bes_files(tmp_path / "src")] == ["real.bes"]

    def test_a_linked_directory_does_not_lead_outside_the_root(self, tmp_path):
        outside = tmp_path / "outside"
        _write(outside, "secret.bes")
        root = tmp_path / "root"
        _write(root, "mine.bes")
        try:
            (root / "escape").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links are not available here")
        assert [p.name for p in bes.bes_files(root)] == ["mine.bes"]

    @pytest.mark.skipif(sys.platform != "win32", reason="junctions exist only on Windows")
    def test_a_junction_does_not_lead_outside_the_root(self, tmp_path):
        outside = tmp_path / "outside"
        _write(outside, "secret.bes")
        root = tmp_path / "root"
        _write(root, "mine.bes")
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(root / "escape"), str(outside)],
            capture_output=True,
            check=False,
        )
        if made.returncode != 0:
            pytest.skip("could not create a junction here")
        assert [p.name for p in bes.bes_files(root)] == ["mine.bes"]


class TestReading:
    def test_nothing_is_written_under_the_source(self, tmp_path):
        _write(tmp_path, "a.bes")
        before = sorted(p.name for p in tmp_path.rglob("*"))
        bes.extract(tmp_path)
        assert sorted(p.name for p in tmp_path.rglob("*")) == before

    def test_an_external_entity_is_not_resolved(self, tmp_path):
        secret = tmp_path / "private.txt"
        secret.write_text("TOPSECRETVALUE", encoding="utf-8")
        hostile = (
            '<?xml version="1.0"?>'
            f'<!DOCTYPE BES [<!ENTITY leak SYSTEM "{secret.as_uri()}">]>'
            "<BES><Fixlet><Title>T</Title><Relevance>&leak;</Relevance></Fixlet></BES>"
        ).encode()
        items = []
        # refusing the file is as safe as reading it without the entity
        with contextlib.suppress(bes.etree.XMLSyntaxError):
            items = bes.parse_bes(hostile, "x.bes")
        assert "TOPSECRETVALUE" not in repr(items)

    def test_a_bad_file_is_recorded_and_the_rest_still_read(self, tmp_path):
        _write(tmp_path, "bad.BES", b"<BES><Fixlet>")
        _write(tmp_path, "good.bes")
        result = bes.extract(tmp_path)
        assert result.unparsable == ["bad.BES"]
        assert [i.path for i in result.items] == ["good.bes"]

"""tools/eval/questions.py: candidate questions from real BES content."""

import json

import pytest
from lxml import etree

from tools.eval import questions as qs

# Fake credentials are assembled from fragments so no real-looking token sits in this file.
AWS_KEY = "".join(("AK", "IA", "ABCDEFGHIJKLMNOP"))


def bes(*bodies):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:noNamespaceSchemaLocation="BES.xsd">' + "".join(bodies) + "</BES>"
    ).encode()


FIXLET = (
    "<Fixlet><Title>Install Foo</Title>"
    "<Description><![CDATA[<p>Installs <b>Foo</b> on Windows.</p>]]></Description>"
    "<Relevance>windows of operating system</Relevance>"
    '<Relevance>not exists file "c:\\foo.exe"</Relevance>'
    "<Category>Software</Category>"
    "<Action><ActionScript>run foo</ActionScript></Action></Fixlet>"
)
TASK = (
    "<Task><Title>Restart</Title><Description>Plain text.</Description>"
    "<Relevance>true</Relevance></Task>"
)


def fixlet(title="T", relevance=("true",), description=""):
    """A minimal Fixlet body for tests that need only one detail to vary."""
    desc = f"<Description>{description}</Description>" if description else ""
    rel = "".join(f"<Relevance>{r}</Relevance>" for r in relevance)
    return f"<Fixlet><Title>{title}</Title>{desc}{rel}</Fixlet>"


ANALYSIS = (
    "<Analysis><Title>Inventory</Title><Description>What is installed.</Description>"
    "<Relevance>windows of operating system</Relevance>"
    '<Property Name="Foo Version" ID="1">version of file "c:\\foo.exe"</Property>'
    '<Property Name="Host" ID="2">name of operating system</Property></Analysis>'
)


class TestParseBes:
    def test_a_fixlet_keeps_its_title_description_and_every_relevance_line(self):
        (item,) = qs.parse_bes(bes(FIXLET), "a.bes")
        assert (item.kind, item.title, item.path) == ("Fixlet", "Install Foo", "a.bes")
        assert item.description == "Installs Foo on Windows."
        assert [(s.role, s.relevance) for s in item.statements] == [
            ("applicability", "windows of operating system"),
            ("applicability", 'not exists file "c:\\foo.exe"'),
        ]

    def test_a_task_is_read_like_a_fixlet(self):
        (item,) = qs.parse_bes(bes(TASK), "t.bes")
        assert (item.kind, item.description) == ("Task", "Plain text.")

    def test_an_analysis_has_applicability_and_named_properties(self):
        (item,) = qs.parse_bes(bes(ANALYSIS), "x.bes")
        assert [(s.role, s.name) for s in item.statements] == [
            ("applicability", ""),
            ("property", "Foo Version"),
            ("property", "Host"),
        ]
        assert item.statements[1].relevance == 'version of file "c:\\foo.exe"'

    def test_action_scripts_are_never_read(self):
        (item,) = qs.parse_bes(bes(FIXLET), "a.bes")
        assert all("run foo" not in s.relevance for s in item.statements)
        assert "run foo" not in item.description

    def test_other_elements_and_items_without_relevance_are_ignored(self):
        body = "<Baseline><Title>B</Title></Baseline><Fixlet><Title>None</Title></Fixlet>"
        assert qs.parse_bes(bes(body), "b.bes") == []

    def test_blank_relevance_elements_are_skipped(self):
        body = (
            "<Fixlet><Title>T</Title><Relevance>  </Relevance><Relevance>true</Relevance></Fixlet>"
        )
        (item,) = qs.parse_bes(bes(body), "b.bes")
        assert [s.relevance for s in item.statements] == ["true"]

    def test_a_long_description_is_cut(self):
        body = fixlet(description="word " * 400)
        (item,) = qs.parse_bes(bes(body), "b.bes")
        assert len(item.description) <= qs.DESCRIPTION_CHARS

    def test_xml_that_is_not_well_formed_raises(self):
        with pytest.raises(etree.XMLSyntaxError):
            qs.parse_bes(b"<BES><Fixlet>", "bad.bes")


class TestHostileXml:
    def test_an_external_entity_does_not_read_a_file(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
        doc = (
            f'<?xml version="1.0"?><!DOCTYPE BES [<!ENTITY leak SYSTEM "{secret.as_uri()}">]>'
            "<BES><Fixlet><Title>&leak;</Title>"
            "<Relevance>true</Relevance></Fixlet></BES>"
        ).encode()
        try:
            items = qs.parse_bes(doc, "x.bes")
        except etree.XMLSyntaxError:
            items = []  # refusing the document is also safe
        assert all("TOP-SECRET-CONTENT" not in item.title for item in items)

    def test_entity_expansion_bombs_are_refused_or_inert(self):
        tens = "&a;" * 10
        hundreds = "&b;" * 10
        bomb = (
            '<?xml version="1.0"?><!DOCTYPE BES [<!ENTITY a "aaaaaaaaaa">'
            f'<!ENTITY b "{tens}"><!ENTITY c "{hundreds}">]>'
            "<BES><Fixlet><Title>&c;</Title><Relevance>true</Relevance></Fixlet></BES>"
        ).encode("ascii")
        try:
            items = qs.parse_bes(bomb, "x.bes")
        except etree.XMLSyntaxError:
            items = []
        assert all(len(item.title) < 100 for item in items)  # never expanded to 1000 characters


class TestExtract:
    def write(self, root, name, body):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode())
        return path

    def test_every_bes_file_under_the_source_is_read_in_a_stable_order(self, tmp_path):
        self.write(tmp_path, "b/two.bes", bes(TASK))
        self.write(tmp_path, "a/one.bes", bes(FIXLET))
        extraction = qs.extract(tmp_path)
        assert extraction.files == 2
        assert [i.path for i in extraction.items] == ["a/one.bes", "b/two.bes"]

    def test_a_byte_identical_copy_counts_once(self, tmp_path):
        self.write(tmp_path, "site/one.bes", bes(FIXLET))
        self.write(tmp_path, "site - Copy/one.bes", bes(FIXLET))
        extraction = qs.extract(tmp_path)
        assert (extraction.files, extraction.duplicate_files, len(extraction.items)) == (2, 1, 1)

    def test_the_same_item_in_different_bytes_counts_once(self, tmp_path):
        self.write(tmp_path, "one.bes", bes(FIXLET))
        self.write(tmp_path, "two.bes", bes(FIXLET) + b"\n")  # different bytes, same item
        extraction = qs.extract(tmp_path)
        assert (extraction.duplicate_files, extraction.duplicate_items) == (0, 1)
        assert len(extraction.items) == 1

    def test_an_unparsable_file_is_reported_not_fatal(self, tmp_path):
        self.write(tmp_path, "bad.bes", b"<BES><Fixlet>")
        self.write(tmp_path, "good.bes", bes(TASK))
        extraction = qs.extract(tmp_path)
        assert extraction.unparsable == ["bad.bes"]
        assert len(extraction.items) == 1

    def test_tooling_and_dependency_directories_are_skipped(self, tmp_path):
        self.write(tmp_path, ".git/x.bes", bes(TASK))
        self.write(tmp_path, ".venv/Lib/site-packages/y.bes", bes(TASK))
        self.write(tmp_path, "keep.bes", bes(FIXLET))
        assert [i.path for i in qs.extract(tmp_path).items] == ["keep.bes"]

    def test_only_bes_files_are_read(self, tmp_path):
        self.write(tmp_path, "notes.xml", bes(FIXLET))
        assert qs.extract(tmp_path).files == 0


class TestCandidates:
    def records(self, tmp_path, *bodies):
        (tmp_path / "x.bes").write_bytes(bes(*bodies))
        records, stats = qs.candidates(qs.extract(tmp_path).items)
        return records, stats

    def test_a_fixlet_gives_one_candidate_holding_all_its_relevance(self, tmp_path):
        (record,), _ = self.records(tmp_path, FIXLET)
        assert record["kind"] == "applicability"
        assert record["item_kind"] == "Fixlet"
        assert record["relevance"] == [
            "windows of operating system",
            'not exists file "c:\\foo.exe"',
        ]
        assert record["dialect"] == "client"
        assert record["parses"] is True
        assert record["title"] == "Install Foo"

    def test_an_analysis_gives_one_applicability_and_one_candidate_per_property(self, tmp_path):
        found, _ = self.records(tmp_path, ANALYSIS)
        assert [(r["kind"], r["name"]) for r in found] == [
            ("applicability", ""),
            ("property", "Foo Version"),
            ("property", "Host"),
        ]
        assert found[1]["relevance"] == ['version of file "c:\\foo.exe"']
        assert found[1]["title"] == "Inventory"  # the analysis title is the property's context

    def test_the_analyzer_verdict_is_recorded(self, tmp_path):
        body = fixlet(relevance=("number of of x", "nonsense of it"))
        (record,), _ = self.records(tmp_path, body)
        assert record["parses"] is False
        body = "<Fixlet><Title>T</Title><Relevance>exists (nonsense of it)</Relevance></Fixlet>"
        (record,), _ = self.records(tmp_path, body)
        assert "nonsense" in record["unknown_inspectors"]

    def test_ids_are_stable_and_distinct(self, tmp_path):
        first, _ = self.records(tmp_path, ANALYSIS)
        again, _ = self.records(tmp_path, ANALYSIS)
        assert [r["id"] for r in first] == [r["id"] for r in again]
        assert len({r["id"] for r in first}) == len(first)

    def test_a_candidate_holding_a_secret_is_dropped_and_counted(self, tmp_path):
        body = f'<Fixlet><Title>T</Title><Relevance>"{AWS_KEY}" = "x"</Relevance></Fixlet>'
        records, stats = self.records(tmp_path, body, FIXLET)
        assert [r["title"] for r in records] == ["Install Foo"]
        assert stats["dropped_secret"] == 1

    def test_a_secret_in_the_description_also_drops_it(self, tmp_path):
        body = fixlet(description=f"key {AWS_KEY}")
        records, stats = self.records(tmp_path, body)
        assert records == []
        assert stats["dropped_secret"] == 1

    def test_a_warning_is_counted_and_the_candidate_kept(self, tmp_path):
        body = fixlet(description="ask jane.doe@corp.test.io")
        records, stats = self.records(tmp_path, body)
        assert len(records) == 1
        assert stats["scan_warnings"] == 1


class TestOutput:
    def test_the_file_is_ascii_sorted_and_reproducible(self, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        body = "<Fixlet><Title>Title" + chr(0x3B1) + "</Title><Relevance>true</Relevance></Fixlet>"
        (source / "x.bes").write_bytes(bes(body))
        outs = []
        for name in ("one", "two"):
            out = tmp_path / name / "candidates.jsonl"
            assert qs.main(["extract", "--source", str(source), "--out", str(out)]) == 0
            outs.append(out.read_bytes())
        assert outs[0] == outs[1]
        text = outs[0].decode("ascii")  # raises if anything non-ASCII was written
        assert json.loads(text)["title"] == "Title" + chr(0x3B1)

    def test_main_prints_an_aggregate_report_without_the_content(self, tmp_path, capsys):
        source = tmp_path / "src"
        source.mkdir()
        (source / "a.bes").write_bytes(bes(FIXLET, ANALYSIS))
        (source / "copy.bes").write_bytes(bes(FIXLET, ANALYSIS))
        assert (
            qs.main(["extract", "--source", str(source), "--out", str(tmp_path / "o.jsonl")]) == 0
        )
        output = capsys.readouterr().out
        assert "files read: 2 (1 identical copies skipped, 0 not parseable)" in output
        assert "applicability: 2" in output
        assert "property: 2" in output
        assert "untracked" in output
        assert "Install Foo" not in output  # the report never echoes the content
        assert "foo.exe" not in output

    def test_a_source_that_is_not_a_directory_is_a_clean_exit_2(self, tmp_path):
        assert (
            qs.main(["extract", "--source", str(tmp_path / "nope"), "--out", str(tmp_path / "o")])
            == 2
        )

    def test_an_empty_source_writes_an_empty_file(self, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        out = tmp_path / "o.jsonl"
        assert qs.main(["extract", "--source", str(source), "--out", str(out)]) == 0
        assert out.read_bytes() == b""

    def test_the_default_output_is_inside_the_untracked_eval_directory(self):
        args = qs._parse_args(["extract", "--source", "x"])
        assert args.out.parts[-3:] == ("eval", "questions", qs.CANDIDATES_NAME)

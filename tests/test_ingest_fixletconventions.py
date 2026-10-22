"""tools/ingest/fixletconventions.py and its place in the fixlet index and build."""

import json
import sqlite3

import pytest

from tools.ingest import fixletbuild, fixletconventions, fixletindex, fixlets
from tools.ingest.fixletindex import ClauseRow

GUARD = 'if exists property "in proxy agent context" then not in proxy agent context else true'


def clause(content_id, lines, *, source="s", role="applicability", platform="windows"):
    return ClauseRow(
        source_id=source,
        path=f"{content_id}.bes",
        file_name=content_id,
        content_id=content_id,
        item_kind="Fixlet",
        role=role,
        property_name="",
        title="t",
        description="",
        action_excerpt="",
        relevance=tuple(lines),
        platform=platform,
    )


def mine(clauses, groups=None, **kwargs):
    return fixletconventions.mine(clauses, groups or {}, **kwargs)


def find(rows, kind, pattern, *, group="s", platform="windows", role="applicability"):
    for row in rows:
        if (row.source_group, row.role, row.platform, row.kind, row.pattern) == (
            group,
            role,
            platform,
            kind,
            pattern,
        ):
            return row
    return None


class TestPlatform:
    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            (["windows of operating system"], "windows"),
            (["Windows of Operating System and true"], "windows"),
            (["mac of operating system"], "mac"),
            (["unix of operating system"], "unix"),
            (['name of operating system as lowercase starts with "win"'], "windows"),
            (['name of operating system contains "Linux"'], "unix"),
            (['name of operating system = "MacOSX"'], "mac"),
            (["windows of operating system", "mac of operating system"], "multi"),
            (["not windows of operating system"], "none"),
            (['exists file "c:\\x"'], "none"),
            ([], "none"),
        ],
    )
    def test_the_platform_comes_from_the_operating_system_test(self, lines, expected):
        assert fixletconventions.platform_of(lines) == expected


class TestNormalizing:
    def test_normalize_lowercases_and_collapses_spaces_but_keeps_literals(self):
        assert fixletconventions.normalize('  Exists   File  "C:\\Temp\\A.txt" ') == (
            'exists file "c:\\temp\\a.txt"'
        )

    def test_shape_replaces_strings_and_numbers(self):
        line = 'exists file "C:\\x.exe" and version of it >= "5.1" and 3 < 10.5'
        assert fixletconventions.shape(line) == (
            'exists file "<str>" and version of it >= "<str>" and <n> < <n>'
        )

    def test_a_number_inside_a_word_is_left_alone(self):
        assert fixletconventions.shape("x86 of it") == "x86 of it"

    def test_conjuncts_split_at_the_top_level_only(self):
        assert fixletconventions.conjuncts('exists file "a and b" and (x = 1 and y = 2)') == [
            'exists file "a and b"',
            "x = 1 and y = 2",
        ]

    def test_a_guard_joined_to_a_platform_test_is_found(self):
        joined = f"windows of operating system and ({GUARD})"
        assert fixletconventions.conjuncts(joined) == ["windows of operating system", GUARD]

    def test_parentheses_around_the_whole_part_are_removed(self):
        assert fixletconventions.conjuncts("(a) and ((b))") == ["a", "b"]

    def test_parentheses_that_close_early_are_kept(self):
        assert fixletconventions.conjuncts("(a) or (b)") == ["(a) or (b)"]


class TestSupport:
    def test_a_pattern_below_the_minimum_is_not_a_convention(self):
        clauses = [clause(f"i{n}", ["a", "b" if n < 4 else "c"]) for n in range(10)]
        rows = mine(clauses, min_files=5, min_share=0.0)
        assert find(rows, "line", "a").files == 10
        assert find(rows, "line", "b") is None  # 4 items
        assert find(rows, "line", "c").files == 6

    def test_the_share_threshold_applies_too(self):
        clauses = [clause(f"i{n}", ["rare" if n < 5 else "common"]) for n in range(100)]
        rows = mine(clauses, min_files=1, min_share=0.10)
        assert find(rows, "line", "rare") is None  # 5 percent
        assert find(rows, "line", "common").share == 0.95

    def test_the_numbers_are_recorded_so_a_claim_can_be_checked(self):
        clauses = [clause(f"i{n}", ["a"]) for n in range(8)]
        row = find(mine(clauses, min_files=5), "line", "a")
        assert (row.files, row.total, row.share) == (8, 8, 1.0)
        assert row.examples == ("i0", "i1", "i2")


class TestRoleThresholds:
    def varied(self, role):
        # 100 items, one pattern held by 3 of them: 3 percent
        return [clause(f"i{n}", ["rare" if n < 3 else f"u{n}"], role=role) for n in range(100)]

    def test_a_property_habit_is_reported_at_a_lower_share_than_an_applicability_one(self):
        rows = mine(self.varied("property") + self.varied("applicability"), min_files=3)
        assert find(rows, "line", "rare", role="property") is not None  # 3% >= 1%
        assert find(rows, "line", "rare", role="applicability") is None  # 3% < 10%

    def test_one_number_applies_to_every_role(self):
        rows = mine(self.varied("applicability"), min_files=3, min_share=0.02)
        assert find(rows, "line", "rare") is not None

    def test_a_mapping_overrides_only_the_roles_it_names(self):
        shares = fixletconventions.resolve_min_share({"applicability": 0.5})
        assert shares == {"applicability": 0.5, "property": fixletconventions.MIN_SHARE["property"]}

    def test_the_defaults_are_a_copy_not_the_shared_table(self):
        fixletconventions.resolve_min_share(None)["property"] = 9
        assert fixletconventions.MIN_SHARE["property"] != 9


class TestCounting:
    def test_an_item_is_counted_once_however_often_it_repeats_a_line(self):
        clauses = [clause(f"i{n}", ["a", "a", "A"]) for n in range(5)]
        assert find(mine(clauses, min_files=1), "line", "a").files == 5

    def test_the_same_item_in_two_sources_of_one_group_counts_once(self):
        clauses = [clause("same", ["a"], source="one"), clause("same", ["a"], source="two")]
        rows = mine(clauses, {"one": "team", "two": "team"}, min_files=1)
        row = find(rows, "line", "a", group="team")
        assert (row.files, row.total) == (1, 1)

    def test_groups_are_never_pooled(self):
        team = [clause(f"t{n}", [GUARD], source="mine") for n in range(6)]
        public = [clause(f"p{n}", [GUARD, "x"], source="theirs") for n in range(60)]
        rows = mine(team + public, {"mine": "team", "theirs": "public"}, min_files=1)
        assert find(rows, "line", GUARD, group="team").files == 6
        assert find(rows, "line", GUARD, group="team").total == 6
        assert find(rows, "line", GUARD, group="public").total == 60

    def test_a_source_without_a_group_counts_alone(self):
        rows = mine([clause("a", ["x"], source="lone")], {}, min_files=1)
        assert rows[0].source_group == "lone"

    def test_platforms_are_counted_separately_and_rolled_up_as_all(self):
        clauses = [clause(f"w{n}", ["a"], platform="windows") for n in range(6)]
        clauses += [clause(f"m{n}", ["a"], platform="mac") for n in range(7)]
        rows = mine(clauses, min_files=1)
        assert find(rows, "line", "a", platform="windows").files == 6
        assert find(rows, "line", "a", platform="mac").files == 7
        assert find(rows, "line", "a", platform="all").files == 13

    def test_roles_are_separate(self):
        clauses = [clause(f"a{n}", ["x"], role="applicability") for n in range(5)]
        clauses += [clause(f"p{n}", ["x"], role="property") for n in range(5)]
        rows = mine(clauses, min_files=1)
        assert find(rows, "line", "x", role="property").total == 5

    def test_the_opening_line_is_the_first_one(self):
        clauses = [clause(f"i{n}", ["first", "second"]) for n in range(5)]
        rows = mine(clauses, min_files=1)
        assert find(rows, "opening", "first").files == 5
        assert find(rows, "opening", "second") is None

    def test_shape_finds_structure_when_the_values_differ(self):
        clauses = [clause(f"i{n}", [f'exists file "c:\\f{n}.exe"']) for n in range(6)]
        rows = mine(clauses, min_files=5)
        assert find(rows, "shape", 'exists file "<str>"').files == 6
        assert find(rows, "line", 'exists file "c:\\f1.exe"') is None

    def test_a_guard_counts_alone_or_joined(self):
        clauses = [clause(f"a{n}", [GUARD]) for n in range(3)]
        clauses += [
            clause(f"b{n}", [f"windows of operating system and ({GUARD})"]) for n in range(4)
        ]
        row = find(mine(clauses, min_files=1), "conjunct", GUARD)
        assert (row.files, row.total) == (7, 7)

    def test_only_the_top_patterns_are_kept_per_kind(self):
        clauses = [clause(f"i{n}", [f"line{n % 10}"]) for n in range(100)]
        rows = mine(clauses, min_files=1, min_share=0.0, top=3)
        assert len([r for r in rows if r.kind == "line" and r.platform == "windows"]) == 3

    def test_the_output_is_deterministic(self):
        clauses = [clause(f"i{n}", ["b", "a", "c"]) for n in range(6)]
        assert mine(clauses, min_files=1) == mine(clauses, min_files=1)


def document(*bodies):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' + "".join(bodies) + "</BES>"
    )


def fixlet_xml(n, *lines):
    rel = "".join(f"<Relevance>{line}</Relevance>" for line in lines)
    return f"<Fixlet><Title>F{n}</Title><Description>d</Description>{rel}</Fixlet>"


def analysis_xml(*props, applicability=("windows of operating system",)):
    rel = "".join(f"<Relevance>{line}</Relevance>" for line in applicability)
    body = "".join(f'<Property Name="{n}">{r}</Property>' for n, r in props)
    return f"<Analysis><Title>A</Title><Description>d</Description>{rel}{body}</Analysis>"


def write_tree(root, files):
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


class TestBuildIntegration:
    def read(self, tmp_path, name, files, **fields):
        tree = write_tree(tmp_path / name, files)
        row = {**fixlets.adhoc_row(directory=str(tree)), **fields}
        read = fixlets.read_source(row, tmp_path / "raw")
        assert read.error == ""
        return read

    def test_the_group_is_recorded_on_the_read(self, tmp_path):
        read = self.read(tmp_path, "t", {"a.bes": document(fixlet_xml(1, "true"))}, group="team")
        built = fixletbuild.assemble([read])
        assert built.data.reads[0].source_group == "team"

    def test_a_source_with_no_group_stands_alone(self, tmp_path):
        read = self.read(tmp_path, "t", {"a.bes": document(fixlet_xml(1, "true"))})
        assert read.record["group"] == read.record["source_id"]

    def test_a_property_with_no_platform_test_takes_its_analysis_platform(self, tmp_path):
        files = {"d.bes": document(analysis_xml(("Free", "free space of drive")))}
        built = fixletbuild.assemble([self.read(tmp_path, "t", files)])
        by_role = {c.role: c.platform for c in built.data.clauses}
        assert by_role == {"applicability": "windows", "property": "windows"}

    def test_a_property_with_its_own_platform_test_keeps_it(self, tmp_path):
        files = {"d.bes": document(analysis_xml(("Mac", "mac of operating system")))}
        built = fixletbuild.assemble([self.read(tmp_path, "t", files)])
        assert {c.platform for c in built.data.clauses if c.role == "property"} == {"mac"}

    def test_conventions_are_counted_per_group_and_stored(self, tmp_path):
        team = {
            f"t{n}.bes": document(fixlet_xml(n, "windows of operating system", GUARD))
            for n in range(6)
        }
        public = {
            f"p{n}.bes": document(fixlet_xml(n + 100, "windows of operating system"))
            for n in range(8)
        }
        reads = [
            self.read(tmp_path, "team", team, group="team", id="team-src"),
            self.read(tmp_path, "pub", public, group="reference", id="pub-src"),
        ]
        built = fixletbuild.assemble(reads, min_files=5)
        guard = [c for c in built.data.conventions if c.pattern == GUARD and c.kind == "conjunct"]
        assert {(c.source_group, c.files, c.total) for c in guard if c.platform == "all"} == {
            ("team", 6, 6)
        }
        out = tmp_path / "f.sqlite"
        fixletindex.write_index(out, built.data)
        meta = dict(sqlite3.connect(out).execute("SELECT key, value FROM meta"))
        assert meta["convention_min_files"] == "5"
        assert int(meta["convention_count"]) == len(built.data.conventions)

    def test_the_index_stores_conventions_with_their_examples(self, tmp_path):
        files = {
            f"t{n}.bes": document(fixlet_xml(n, "windows of operating system")) for n in range(6)
        }
        built = fixletbuild.assemble([self.read(tmp_path, "t", files, group="team")], min_files=5)
        out = tmp_path / "f.sqlite"
        fixletindex.write_index(out, built.data)
        db = sqlite3.connect(out)
        row = db.execute(
            "SELECT files, total, share, examples FROM conventions"
            " WHERE kind = 'line' AND platform = 'all' AND source_group = 'team'"
        ).fetchone()
        assert row[:3] == (6, 6, 1.0)
        assert len(json.loads(row[3])) == 3
        assert db.execute("PRAGMA user_version").fetchone()[0] == fixletindex.SCHEMA_VERSION

    def test_a_bad_group_name_is_refused(self):
        row = {**fixlets.adhoc_row(directory="/x"), "group": "has space"}
        with pytest.raises(ValueError, match="invalid group"):
            fixlets.validate_row(row)

    def test_the_command_line_can_show_the_top_conventions(self, capsys, tmp_path):
        tree = write_tree(
            tmp_path / "tree",
            {f"t{n}.bes": document(fixlet_xml(n, "windows of operating system")) for n in range(6)},
        )
        code = fixletbuild.main(
            [
                "--dir",
                str(tree),
                "--group",
                "team",
                "--dry-run",
                "--show-conventions",
                "3",
                "--raw-dir",
                str(tmp_path / "raw"),
                "--scan-allow",
                str(tmp_path / "none.csv"),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "team / applicability (6 items)" in out
        assert "windows of operating system" in out

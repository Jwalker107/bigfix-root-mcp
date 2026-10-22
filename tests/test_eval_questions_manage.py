"""tools/eval/questions.py: the questions file, its validation and the review commands."""

from typing import ClassVar

import pytest

from tools.eval import questions as qs


def question(identifier="q1", **overrides):
    record = {
        "id": identifier,
        "question": "Write relevance that is true on Windows computers.",
        "kind": "applicability",
        "dialect": "client",
        "origin": "example_site",
        "status": "draft",
        "difficulty": "easy",
        "candidate_id": "c1",
        "reference": [],
        "notes": "",
    }
    record.update(overrides)
    return record


class TestValidateQuestions:
    def test_a_sound_file_has_no_problems(self):
        assert qs.validate_questions([question()], {"c1"}) == []

    def test_a_question_with_its_own_reference_needs_no_candidate(self):
        record = question(candidate_id="", reference=["windows of operating system"])
        assert qs.validate_questions([record], set()) == []

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"question": "   "}, "empty question"),
            ({"id": ""}, "missing id"),
            ({"status": "maybe"}, "status must be one of"),
            ({"kind": "essay"}, "kind must be one of"),
            ({"dialect": "server"}, "dialect must be one of"),
            ({"difficulty": "brutal"}, "difficulty must be one of"),
            ({"reference": "not a list"}, "reference must be a list"),
            ({"reference": [1, 2]}, "reference must be a list"),
            ({"candidate_id": "gone"}, "not in the candidates file"),
            ({"candidate_id": "", "reference": []}, "needs a candidate_id or an inline reference"),
            ({"surprise": 1}, "unknown field 'surprise'"),
        ],
    )
    def test_each_kind_of_mistake_is_reported(self, overrides, message):
        problems = qs.validate_questions([question(**overrides)], {"c1"})
        assert any(message in p for p in problems), problems

    def test_a_duplicate_id_is_reported(self):
        problems = qs.validate_questions([question("a"), question("a")], {"c1"})
        assert any("duplicate id" in p for p in problems)

    def test_candidate_ids_are_only_checked_when_a_candidates_file_exists(self):
        assert qs.validate_questions([question(candidate_id="anything")], None) == []

    def test_a_blank_difficulty_is_allowed(self):
        assert qs.validate_questions([question(difficulty="")], {"c1"}) == []


class TestQuestionFile:
    def test_a_missing_file_is_an_empty_list(self, tmp_path):
        assert qs.load_questions(tmp_path / "none.jsonl") == []

    def test_round_trip_keeps_order_and_is_ascii(self, tmp_path):
        path = tmp_path / "q.jsonl"
        records = [question("b"), question("a", question="text" + chr(0x3B1))]
        qs.write_questions(records, path)
        path.read_bytes().decode("ascii")  # raises on any non-ASCII byte
        assert qs.load_questions(path) == records

    def test_a_bad_line_names_its_line_number(self, tmp_path):
        path = tmp_path / "q.jsonl"
        path.write_text('{"id": "a"}\nnot json\n', encoding="utf-8")
        with pytest.raises(ValueError, match=r"q\.jsonl:2: not valid JSON"):
            qs.load_questions(path)

    def test_a_line_that_is_not_an_object_is_rejected(self, tmp_path):
        path = tmp_path / "q.jsonl"
        path.write_text("[1, 2]\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"q\.jsonl:1: expected a JSON object"):
            qs.load_questions(path)

    def test_blank_lines_are_ignored(self, tmp_path):
        path = tmp_path / "q.jsonl"
        path.write_text('{"id": "a"}\n\n{"id": "b"}\n', encoding="utf-8")
        assert [r["id"] for r in qs.load_questions(path)] == ["a", "b"]


class TestManageQuestions:
    @pytest.fixture
    def files(self, tmp_path):
        candidates_path = tmp_path / "candidates.jsonl"
        qs.write_questions([{"id": "c1"}, {"id": "c2"}], candidates_path)
        questions_path = tmp_path / "questions.jsonl"
        qs.write_questions([question("q1"), question("q2", candidate_id="c2")], questions_path)
        args = ["--questions", str(questions_path), "--candidates", str(candidates_path)]
        return args, questions_path

    def test_summary_counts_without_printing_any_question_text(self, files, capsys):
        args, _ = files
        assert qs.main(["summary", *args]) == 0
        output = capsys.readouterr().out
        assert "questions: 2" in output
        assert "by status: draft 2" in output
        assert "by kind: applicability 2" in output
        assert "problems: 0" in output
        assert "Write relevance" not in output

    def test_summary_fails_and_names_the_problem_when_the_file_is_not_sound(self, files, capsys):
        args, path = files
        qs.write_questions([question("q1", candidate_id="gone")], path)
        assert qs.main(["summary", *args]) == 1
        assert "not in the candidates file" in capsys.readouterr().out

    def test_update_changes_only_the_named_fields_of_one_question(self, files):
        args, path = files
        code = qs.main(["update", *args, "--id", "q1", "--status", "approved", "--notes", "ok"])
        assert code == 0
        first, second = qs.load_questions(path)
        assert (first["status"], first["notes"], first["difficulty"]) == ("approved", "ok", "easy")
        assert second["status"] == "draft"

    def test_update_can_rewrite_the_question_text(self, files):
        args, path = files
        qs.main(["update", *args, "--id", "q2", "--question", "A better question."])
        assert qs.load_questions(path)[1]["question"] == "A better question."

    def test_update_of_an_unknown_id_changes_nothing(self, files):
        args, path = files
        before = path.read_bytes()
        assert qs.main(["update", *args, "--id", "nope", "--status", "approved"]) == 2
        assert path.read_bytes() == before

    def test_update_that_would_make_the_file_invalid_is_not_saved(self, files, capsys):
        args, path = files
        before = path.read_bytes()
        assert qs.main(["update", *args, "--id", "q1", "--question", "  "]) == 2
        assert "not saved" in capsys.readouterr().out
        assert path.read_bytes() == before

    def test_add_writes_an_approved_manual_question_with_its_own_reference(self, files, capsys):
        args, path = files
        code = qs.main(
            [
                "add",
                *args,
                "--question",
                "How do I list services?",
                "--kind",
                "property",
                "--reference",
                "names of services",
                "--difficulty",
                "easy",
            ]
        )
        assert code == 0
        assert "added manual-001" in capsys.readouterr().out
        added = qs.load_questions(path)[-1]
        origin = (added["id"], added["origin"], added["status"])
        assert origin == ("manual-001", "manual", "approved")
        assert added["reference"] == ["names of services"]
        assert added["candidate_id"] == ""

    def test_add_numbers_manual_questions_in_sequence(self, files):
        args, path = files
        for text in ("one", "two"):
            qs.main(["add", *args, "--question", text, "--reference", "true"])
        assert [r["id"] for r in qs.load_questions(path)][-2:] == ["manual-001", "manual-002"]

    def test_add_without_a_reference_is_refused(self, files, capsys):
        args, path = files
        before = path.read_bytes()
        assert qs.main(["add", *args, "--question", "No reference"]) == 2
        assert "needs a candidate_id or an inline reference" in capsys.readouterr().out
        assert path.read_bytes() == before

    def test_a_malformed_questions_file_is_a_clean_exit_2(self, tmp_path):
        path = tmp_path / "q.jsonl"
        path.write_text("garbage\n", encoding="utf-8")
        assert qs.main(["summary", "--questions", str(path)]) == 2

    def test_the_summary_of_a_missing_file_is_empty_not_an_error(self, tmp_path, capsys):
        missing = ["--questions", str(tmp_path / "none.jsonl"), "--candidates", str(tmp_path / "x")]
        assert qs.main(["summary", *missing]) == 0
        assert "questions: 0" in capsys.readouterr().out

    def test_the_default_questions_file_is_inside_the_untracked_eval_directory(self):
        args = qs._parse_args(["summary"])
        assert args.questions.parts[-3:] == ("eval", "questions", qs.QUESTIONS_NAME)


class TestReview:
    CANDIDATES: ClassVar[dict[str, dict]] = {
        "c1": {
            "id": "c1",
            "title": "Install Foo",
            "name": "",
            "relevance": ["windows of operating system", "true"],
        },
        "c2": {
            "id": "c2",
            "title": "Inventory",
            "name": "Foo Version",
            "relevance": ['version of file "x"'],
        },
    }

    def test_each_question_is_shown_beside_its_reference(self):
        text = qs.render_review([question("q1", question="Is it Windows?")], self.CANDIDATES)
        assert "## 1. q1 (applicability, easy, draft)" in text
        assert "**Question:** Is it Windows?" in text
        assert "**Comes from:** Install Foo" in text
        assert "windows of operating system\ntrue" in text

    def test_a_property_names_its_analysis_and_property(self):
        text = qs.render_review(
            [question("q2", candidate_id="c2", kind="property")], self.CANDIDATES
        )
        assert "**Comes from:** Inventory / Foo Version" in text

    def test_a_manual_question_shows_its_inline_reference(self):
        record = question(
            "manual-001", candidate_id="", reference=["names of services"], origin="manual"
        )
        text = qs.render_review([record], {})
        assert "names of services" in text
        assert "**Comes from:**" not in text

    def test_a_missing_reference_is_said_plainly(self):
        text = qs.render_review([question("q1", candidate_id="gone")], self.CANDIDATES)
        assert "(none found)" in text

    def test_notes_appear_only_when_there_are_some(self):
        assert "**Notes:**" not in qs.render_review([question()], self.CANDIDATES)
        assert "**Notes:** check this" in qs.render_review(
            [question(notes="check this")], self.CANDIDATES
        )

    def test_the_sheet_says_it_is_company_content_and_counts_statuses(self):
        text = qs.render_review([question("a"), question("b", status="approved")], self.CANDIDATES)
        assert "do not commit or share" in text
        assert "2 questions: 1 approved, 1 draft" in text

    def test_review_command_writes_the_sheet_and_can_filter_by_status(self, tmp_path, capsys):
        candidates = tmp_path / "candidates.jsonl"
        qs.write_questions(list(self.CANDIDATES.values()), candidates)
        questions_path = tmp_path / "questions.jsonl"
        qs.write_questions(
            [question("q1"), question("q2", status="approved", candidate_id="c2")], questions_path
        )
        out = tmp_path / "review.md"
        args = [
            "review",
            "--questions",
            str(questions_path),
            "--candidates",
            str(candidates),
            "--out",
            str(out),
        ]
        assert qs.main([*args, "--status", "draft"]) == 0
        sheet = out.read_text(encoding="utf-8")
        assert "q1" in sheet
        assert "q2" not in sheet
        assert "wrote 1 questions" in capsys.readouterr().out
        assert qs.main(args) == 0
        assert "q2" in out.read_text(encoding="utf-8")

    def test_the_default_sheet_is_inside_the_untracked_eval_directory(self):
        args = qs._parse_args(["review"])
        assert args.out.parts[-3:] == ("eval", "questions", qs.REVIEW_NAME)

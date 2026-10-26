"""conventions.py: HTML comments in the file are for people and are not sent."""

from pathlib import Path

import pytest

from bigfix_root_mcp import conventions

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def conventions_file(tmp_path, monkeypatch):
    def write(text):
        path = tmp_path / "conventions.md"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setenv(conventions.CONVENTIONS_ENV, str(path))
        return path

    return write


class TestComments:
    def test_a_comment_is_removed_and_the_rules_stay(self, conventions_file):
        conventions_file("<!-- note to editors -->\n\n# Rules\n\nDo the thing.\n")
        text, problem = conventions.load()
        assert problem == ""
        assert "note to editors" not in text
        assert text == "# Rules\n\nDo the thing."

    def test_a_multiline_comment_is_removed_whole(self, conventions_file):
        conventions_file("<!--\nline one\nline two\n-->\nKeep this.\n")
        assert conventions.load()[0] == "Keep this."

    def test_several_comments_are_removed(self, conventions_file):
        conventions_file("A <!-- x --> B\n<!-- y -->\nC\n")
        text = conventions.load()[0]
        assert "x" not in text.replace("A  B", "")
        assert "A  B" in text
        assert text.endswith("C")

    def test_the_blank_lines_a_comment_leaves_behind_are_collapsed(self, conventions_file):
        conventions_file("First.\n\n<!-- gone -->\n\n\n\nSecond.\n")
        assert conventions.load()[0] == "First.\n\nSecond."

    def test_an_unterminated_comment_is_left_alone_not_swallowing_the_rest(self, conventions_file):
        conventions_file("Rule one.\n<!-- never closed\nRule two.\n")
        text = conventions.load()[0]
        assert "Rule one." in text
        assert "Rule two." in text

    def test_a_file_that_is_only_comments_means_no_conventions(self, conventions_file):
        conventions_file("<!-- nothing here yet -->\n")
        assert conventions.load() == ("", "")
        assert conventions.section() == ""

    def test_the_section_never_carries_the_comment(self, conventions_file):
        conventions_file("<!-- for people -->\nUse the guard.\n")
        section = conventions.section()
        assert "Use the guard." in section
        assert "for people" not in section

    def test_the_length_cap_applies_to_what_is_sent(self, conventions_file):
        conventions_file("<!-- " + "x" * 20000 + " -->\n" + "y" * 100)
        text, problem = conventions.load()
        assert problem == ""
        assert text == "y" * 100  # a long comment does not eat the prompt budget

    def test_the_file_size_limit_still_counts_the_comment(self, conventions_file):
        conventions_file("<!-- " + "x" * (conventions.MAX_FILE_BYTES + 10) + " -->\nRule.")
        text, problem = conventions.load()
        assert text == ""
        assert "the limit is" in problem

    def test_text_without_a_comment_is_unchanged(self, conventions_file):
        conventions_file("# Rules\n\nA < B and C > D.\n")
        assert conventions.load()[0] == "# Rules\n\nA < B and C > D."


class TestTheShippedDefault:
    def shipped(self, monkeypatch):
        monkeypatch.setenv(conventions.CONVENTIONS_ENV, str(REPO / "corpus" / "conventions.md"))
        return conventions.load()

    def test_it_says_in_a_comment_that_it_is_an_editable_default(self):
        raw = (REPO / "corpus" / "conventions.md").read_text(encoding="utf-8")
        assert raw.startswith("<!--")
        assert "editable default" in raw.split("-->")[0]

    def test_that_note_is_for_people_and_never_reaches_the_prompt(self, monkeypatch):
        text, problem = self.shipped(monkeypatch)
        assert problem == ""
        assert "editable default" not in text
        assert "<!--" not in text

    def test_the_rules_it_sends_are_still_there(self, monkeypatch):
        text = self.shipped(monkeypatch)[0]
        assert text.startswith("# House relevance conventions")
        assert "in proxy agent context" in text
        assert "otherwise, and say which one you set aside" in text

    def test_it_is_ascii(self):
        assert (REPO / "corpus" / "conventions.md").read_bytes().isascii()

    def test_it_is_tracked_not_ignored(self):
        ignore = (REPO / ".gitignore").read_text(encoding="utf-8")
        assert "corpus/conventions.md" not in ignore

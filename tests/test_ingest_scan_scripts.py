"""tools/ingest/scan.py: the rules added for action scripts and download lines."""

import pytest

from tools.ingest import scan

# Fake credentials are assembled from fragments so no real-looking secret sits in this file.
PASSWORD = "".join(("hunt", "er22x"))
TOKEN = "".join(("abcdefgh", "ijklmnop", "12345678"))


def rules(text):
    return {(f.rule, f.severity) for f in scan.scan_text("s", "p", text)}


class TestUrlCredentials:
    def test_a_password_in_a_url_blocks(self):
        text = f"prefetch f.msi https://deploy:{PASSWORD}@files.internal/f.msi"
        assert ("url-credentials", scan.BLOCK) in rules(text)

    def test_a_plain_url_does_not(self):
        assert rules("prefetch f.msi https://example.com/dir/f.msi") == set()

    def test_a_user_name_alone_is_not_a_password(self):
        assert ("url-credentials", scan.BLOCK) not in rules("clone https://git@host/org/repo")

    def test_a_port_is_not_a_password(self):
        assert rules("get http://host.internal:8080/path") == set()

    def test_a_password_marked_example_is_skipped_like_the_other_rules(self):
        text = "curl https://user:EXAMPLEPASSWORD@host.invalid/x"
        assert ("url-credentials", scan.BLOCK) not in rules(text)


class TestAuthorizationHeader:
    @pytest.mark.parametrize("scheme", ["Bearer", "bearer", "Basic"])
    def test_a_token_in_the_header_blocks(self, scheme):
        text = f'curl -H "Authorization: {scheme} {TOKEN}" https://host/api'
        assert ("authorization-header", scan.BLOCK) in rules(text)

    def test_a_short_value_is_not_treated_as_a_token(self):
        assert ("authorization-header", scan.BLOCK) not in rules("Authorization: Bearer abc")

    def test_the_word_alone_is_not_a_finding(self):
        assert rules("Authorization: required for this call") == set()


class TestPasswordArgument:
    @pytest.mark.parametrize("form", ["--password", "-password", "/password"])
    def test_a_literal_password_argument_warns(self, form):
        assert ("password-argument", scan.WARN) in rules(f"tool.exe {form} {PASSWORD}")

    def test_an_equals_form_warns(self):
        assert ("password-argument", scan.WARN) in rules(f"tool.exe --password={PASSWORD}")

    def test_it_only_warns_and_never_blocks(self):
        assert {s for _, s in rules(f"tool.exe --password {PASSWORD}")} == {scan.WARN}

    @pytest.mark.parametrize("value", ["{parameter 1}", "%PASSWORD%", "$PASSWORD", "<secret>"])
    def test_a_substitution_or_variable_is_not_a_literal(self, value):
        assert ("password-argument", scan.WARN) not in rules(f"tool.exe --password {value}")


class TestTheRulesAreRegistered:
    def test_the_new_names_can_be_allowlisted(self, tmp_path):
        path = tmp_path / "allow.csv"
        path.write_text(
            "source_id,path,rule,reason\ns,a.bes,url-credentials,a documented example\n",
            encoding="utf-8",
        )
        assert ("s", "a.bes", "url-credentials") in scan.read_allowlist(path)

    def test_every_rule_name_is_listed(self):
        for name in ("url-credentials", "authorization-header", "password-argument"):
            assert name in scan.RULE_NAMES

"""tools/ingest/scan.py: heuristic secret and PII scan."""

import pytest

from tools.ingest import scan


# Fake credentials are assembled from fragments so no real-looking token sits in
# this file's source (repository secret scanners read it too).
def assemble(*parts):
    return "".join(parts)


AWS_KEY = assemble("AK", "IA", "ABCDEFGHIJKLMNOP")
GITHUB_TOKEN = assemble("gh", "p_", "a" * 36)
SLACK_TOKEN = assemble("xo", "xb-", "1234567890-abcdefghij")
JWT = assemble("ey", "J", "a" * 12, ".", "ey", "J", "b" * 12, ".", "c" * 12)
PRIVATE_KEY = assemble("-----BEGIN ", "RSA PRIVATE KEY-----")


def rules(text):
    return {(f.rule, f.severity) for f in scan.scan_text("src", "a.md", text)}


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (PRIVATE_KEY, "private-key"),
        (f"key = {AWS_KEY}", "aws-access-key"),
        (f"token {GITHUB_TOKEN}", "github-token"),
        (f"hook {SLACK_TOKEN}", "slack-token"),
        (f"Bearer {JWT}", "jwt"),
    ],
)
def test_high_confidence_credentials_block(text, rule):
    assert (rule, scan.BLOCK) in rules(text)


def test_vendor_example_keys_are_not_flagged():
    assert rules(assemble("AKIA", "IOSFODNN7", "EXAMPLE")) == set()


def test_email_addresses_warn_but_placeholder_domains_do_not():
    assert ("email-address", scan.WARN) in rules("contact jane.doe@corp.test.io for access")
    assert rules("write to someone@example.com or a@example.org") == set()


def test_credential_assignments_warn_but_placeholders_do_not():
    assert ("credential-assignment", scan.WARN) in rules("password = hunter2hunter2")
    assert rules("password = <your password>") == set()
    assert rules("password: ${PASSWORD}") == set()
    assert rules("the password inspector returns a string") == set()


def test_an_example_key_does_not_hide_a_real_one_on_the_same_line():
    example = assemble("AKIA", "IOSFODNN7", "EXAMPLE")
    assert rules(f"{example} then {AWS_KEY}") == {("aws-access-key", "block")}


def test_clean_documentation_has_no_findings():
    assert (
        rules('# bit set\n\nReturns the value of the bit.\n\nQ: bit 0 of bit set "10"\n') == set()
    )


def test_findings_give_location_and_never_the_matched_text():
    findings = scan.scan_text("src", "docs/a.md", f"line one\nsecret line {AWS_KEY}\n")
    assert len(findings) == 1
    finding = findings[0]
    assert (finding.source_id, finding.path, finding.line) == ("src", "docs/a.md", 2)
    assert AWS_KEY not in repr(finding)


class TestAllowlist:
    def write(self, tmp_path, text):
        path = tmp_path / "SCAN_ALLOW.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_missing_file_allows_nothing(self, tmp_path):
        assert scan.read_allowlist(tmp_path / "none.csv") == set()

    def test_matching_findings_are_suppressed_by_path_or_wildcard(self, tmp_path):
        allowed = scan.read_allowlist(
            self.write(
                tmp_path,
                "source_id,path,rule,reason\n"
                "s,a.md,email-address,documented contact\n"
                "s,*,credential-assignment,doc sample\n",
            )
        )
        findings = [
            scan.Finding("s", "a.md", 1, "email-address", scan.WARN),
            scan.Finding("s", "b.md", 1, "email-address", scan.WARN),
            scan.Finding("s", "z.md", 9, "credential-assignment", scan.WARN),
        ]
        kept = scan.apply_allowlist(findings, allowed)
        assert [(f.path, f.rule) for f in kept] == [("b.md", "email-address")]

    def test_a_row_needs_a_reason_and_a_known_rule(self, tmp_path):
        with pytest.raises(ValueError, match="needs a reason"):
            scan.read_allowlist(self.write(tmp_path, "source_id,path,rule,reason\ns,a.md,jwt,\n"))
        with pytest.raises(ValueError, match="unknown rule"):
            scan.read_allowlist(
                self.write(tmp_path, "source_id,path,rule,reason\ns,a.md,nope,why\n")
            )

    def test_missing_columns_are_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="missing column"):
            scan.read_allowlist(self.write(tmp_path, "source_id,rule\ns,jwt\n"))

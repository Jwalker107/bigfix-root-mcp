"""Heuristic secret and PII scan run on source text before it is indexed.

Two severities:

  block  high-confidence credential formats (private keys, cloud and token
         prefixes). The build refuses to write an index while any is present.
  warn   things worth a look but common in harmless documentation: email
         addresses and `password = value` style assignments. Reported only.

This is a safety net, not a guarantee. It will miss secrets that do not have a
recognisable shape, so it does not replace reviewing what goes into the corpus.

Findings carry the rule and location only, never the matched text, so a report
or log cannot leak the thing it found.

A finding that is a known false positive can be suppressed with a row in
corpus/SCAN_ALLOW.csv (source_id, path, rule, reason). Use `*` for any path.
"""

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

BLOCK = "block"
WARN = "warn"

ALLOW_COLUMNS = ("source_id", "path", "rule", "reason")

# (rule name, severity, pattern)
_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "private-key",
        BLOCK,
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY"),
    ),
    ("aws-access-key", BLOCK, re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", BLOCK, re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-fine-grained-token", BLOCK, re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("slack-token", BLOCK, re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", BLOCK, re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "jwt",
        BLOCK,
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    (
        "credential-assignment",
        WARN,
        re.compile(
            r"\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b"
            r"\s*[:=]\s*[\"']?(?![<{$%*\"'\s])[^\s\"'<>]{8,}",
            re.IGNORECASE,
        ),
    ),
    (
        "email-address",
        WARN,
        re.compile(
            r"\b[A-Za-z0-9._%+-]+@(?!(?:[A-Za-z0-9-]+\.)*(?:example\.(?:com|org|net)|localhost)\b)"
            r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
        ),
    ),
)

RULE_NAMES = tuple(name for name, _, _ in _RULES)


@dataclass(frozen=True)
class Finding:
    source_id: str
    path: str
    line: int
    rule: str
    severity: str


def scan_text(source_id: str, path: str, text: str) -> list[Finding]:
    """Scan one file's text. Matches containing the word EXAMPLE are skipped.

    Vendors mark documentation keys that way (for example AWS's
    `AKIAIOSFODNN7EXAMPLE`), so those are known not to be real.
    """
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, severity, pattern in _RULES:
            match = pattern.search(line)
            if match and "EXAMPLE" not in match.group(0):
                findings.append(Finding(source_id, path, number, rule, severity))
    return findings


def read_allowlist(path: Path) -> set[tuple[str, str, str]]:
    """Read (source_id, path, rule) triples to suppress. A missing file allows nothing."""
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in ALLOW_COLUMNS if name not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing column(s): {', '.join(missing)}")
        allowed = set()
        for row in reader:
            rule = (row["rule"] or "").strip()
            if rule not in RULE_NAMES:
                raise ValueError(f"{path}: unknown rule {rule!r}")
            if not (row["reason"] or "").strip():
                raise ValueError(f"{path}: an allowlist row for rule {rule!r} needs a reason")
            allowed.add(((row["source_id"] or "").strip(), (row["path"] or "").strip(), rule))
        return allowed


def apply_allowlist(
    findings: Iterable[Finding], allowed: set[tuple[str, str, str]]
) -> list[Finding]:
    """Drop findings the allowlist covers."""
    return [
        finding
        for finding in findings
        if (finding.source_id, finding.path, finding.rule) not in allowed
        and (finding.source_id, "*", finding.rule) not in allowed
    ]

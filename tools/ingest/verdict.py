"""The offline analyzer's verdict on one relevance query.

Shared by the build, which stores the verdict with each example in the index, and
by tools/eval, which measures it. Nothing here contacts BigFix.

The verdict is evidence, not authority. The analyzer's inspector table is a
snapshot, so "unknown inspector" can mean the documentation is newer than the
snapshot as easily as it can mean a mistake, and "does not parse" is a statement
about the analyzer's grammar. An analyzer failure never stops a build: it is
recorded in the verdict (`crashed`) and the example is stored unjudged.
"""

from dataclasses import dataclass

import bigfix_relevance_analyzer as analyzer
from bigfix_relevance_analyzer import (
    Dialect,
    LintConfig,
    analyze_relevance,
    is_definite,
    lint_analysis,
)

PINNABLE = {"session": Dialect.SESSION, "client": Dialect.CLIENT}
# the dialects an example can be labelled with from the analyzer's evidence
LABELS = frozenset(PINNABLE)


@dataclass(frozen=True)
class Verdict:
    parsed: bool = False
    parse_error: str = ""
    effective: str = ""  # the dialect the analyzer settled on
    classified: str = ""  # from the text alone, before parsing
    resolved: str = ""  # from the inspector table, after parsing
    mismatch: bool = False  # the query's own evidence contradicts the pinned dialect
    findings: tuple[tuple[str, str], ...] = ()  # (code, severity)
    unknown_inspectors: tuple[str, ...] = ()
    crashed: str = ""  # the analyzer itself raised; the verdict is empty and means nothing


def analyzer_version() -> str:
    """The analyzer's version, recorded in the index for reproducibility."""
    return str(getattr(analyzer, "__version__", ""))


def _value(dialect: Dialect | None) -> str:
    return "" if dialect is None else dialect.value


def analyze_query(query: str, pinned: str = "") -> Verdict:
    """Analyze one query, with its dialect pinned to 'session' or 'client' when known."""
    pinned_dialect = PINNABLE.get(pinned)
    try:
        report = analyze_relevance(query, dialect=pinned_dialect)
        findings = lint_analysis(report, LintConfig(dialect=pinned_dialect, suggest=False))
    except Exception as err:  # the analyzer must never be the reason a run stops
        return Verdict(crashed=f"{type(err).__name__}: {err}")
    mismatch = pinned_dialect is not None and any(
        observed is not None and is_definite(observed) and observed is not pinned_dialect
        for observed in (report.classified_dialect, report.resolved_dialect)
    )
    return Verdict(
        parsed=bool(report.parsed),
        parse_error="" if report.parse_error is None else str(report.parse_error),
        effective=_value(report.dialect),
        classified=_value(report.classified_dialect),
        resolved=_value(report.resolved_dialect),
        mismatch=mismatch,
        findings=tuple((finding.code, finding.severity.value) for finding in findings),
        unknown_inspectors=tuple(report.unknown_references),
    )


def inferred_dialect(verdict: Verdict) -> str:
    """'session' or 'client' when the analyzer is definite and consistent, else ''.

    Both signals (the text classifier and the inspector table) are consulted. If
    either names a dialect, that is the answer unless the other names the opposite
    one, and a query that does not parse is never labelled: its evidence is
    exactly what could not be trusted.
    """
    if verdict.crashed or not verdict.parsed:
        return ""
    named = {verdict.classified, verdict.resolved} & LABELS
    return next(iter(named)) if len(named) == 1 else ""

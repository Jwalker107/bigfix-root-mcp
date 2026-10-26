"""Count the habits in a body of reference fixlets.

A convention here is something that can be checked: "this line appears in 412 of 430
Windows applicability sets". Nothing is summarized or guessed by a model, so every
claim has its numbers and its example ids, and the same input always gives the same
output.

Five kinds of pattern are counted, for each (group, role, platform):

  line     a relevance line, normalized (lower case, spaces collapsed, string literals
           kept). Counted once per item however often the item repeats it.
  conjunct a top-level `and` part of a line, so a guard counts whether it stands on its
           own line or is joined to a platform test with `and`.
  opening  the first line of an applicability set, or of a property's relevance, as
           the line that opens it is usually the guard or the platform test.
  shape    a line with its string literals and numbers replaced by placeholders, so
           the structure (`exists file <str>`) shows through when the values differ.

A pattern is reported only above a minimum support (at least `min_files` items and at
least `min_share` of its group), so a one-off quirk is not called a convention. The
share is set per role, because properties vary far more than applicability sets.

Groups are never pooled. A body of public content must not be reported as a team's
habits, so each group is counted on its own. The platform "all" rolls a group's
platforms together, for rules that hold everywhere.

The platform comes from the item's own operating-system test and is a heuristic: it
reads `windows of operating system`, `mac of operating system`, `unix of operating
system` and the common spellings of a name test. A negated test is not counted.

What is counted is what was written, mistakes included. These are observations, not
approvals, and they never override a rule someone declared.
"""

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tools.ingest import actiontemplate
from tools.ingest.fixletindex import ClauseRow, ConventionRow

MIN_FILES = 5
# applicability sets repeat a few guards, so a habit shows in a tenth of them; properties
# are far more varied (the commonest shape is about 2 percent), so they get a lower bar
MIN_SHARE = {"applicability": 0.10, "property": 0.01, "actionscript": 0.05}
TOP_PER_KIND = 50
PATTERN_CHARS = 300
EXAMPLES = 3

KINDS = ("line", "conjunct", "command", "opening", "shape")
# which patterns are counted for which role: an `and` part means something in relevance,
# the command word that opens a line means something in an action script
KINDS_BY_ROLE = {
    "applicability": ("line", "conjunct", "opening", "shape"),
    "property": ("line", "conjunct", "opening", "shape"),
    "actionscript": ("line", "command", "opening", "shape"),
}
PLATFORM_ALL = "all"
PLATFORM_NONE = "none"
PLATFORM_MULTI = "multi"

_NAME_TEST = r"\bname of operating system\b[^\n]*?"
_PLATFORMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "windows",
        re.compile(rf"(?<!not )\bwindows of operating system\b|{_NAME_TEST}\bwin", re.IGNORECASE),
    ),
    (
        "mac",
        re.compile(rf"(?<!not )\bmac of operating system\b|{_NAME_TEST}\b(?:mac|darwin)", re.I),
    ),
    (
        "unix",
        re.compile(
            rf"(?<!not )\bunix of operating system\b|"
            rf"{_NAME_TEST}\b(?:linux|rhel|red hat|ubuntu|debian|suse|solaris|aix|hp-ux)",
            re.IGNORECASE,
        ),
    ),
)

_STRING = re.compile(r'"[^"\n]*"')
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)*(?![\w.])")
_URL = re.compile(r"https?://[^\s\"'<>]+")
_HASH = re.compile(r"\b[0-9a-f]{32,}\b")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class _Unit:
    """One distinct item in one group, with the lines it says."""

    group: str
    role: str
    platform: str
    content_id: str
    lines: tuple[str, ...]


def resolve_min_share(value: float | Mapping[str, float] | None) -> dict[str, float]:
    """A minimum share for every role, from one number, a per-role mapping or the defaults."""
    if value is None:
        return dict(MIN_SHARE)
    if isinstance(value, Mapping):
        return {**MIN_SHARE, **value}
    return {role: float(value) for role in MIN_SHARE}


def platform_of(lines: Sequence[str]) -> str:
    """windows, mac, unix, multi (more than one), or none (no operating-system test)."""
    text = "\n".join(lines)
    found = [name for name, pattern in _PLATFORMS if pattern.search(text)]
    if not found:
        return PLATFORM_NONE
    return found[0] if len(found) == 1 else PLATFORM_MULTI


def normalize(line: str) -> str:
    """Lower case with spaces collapsed; string literals are kept as written."""
    return _SPACES.sub(" ", line).strip().lower()


def shape(line: str) -> str:
    """A normalized line with its string literals and numbers replaced by placeholders."""
    text = _STRING.sub('"<str>"', normalize(line))
    return _NUMBER.sub("<n>", _HASH.sub("<hash>", _URL.sub("<url>", text)))


def _top_level_parts(text: str) -> list[str]:
    """Split on ` and ` outside quotes and parentheses."""
    parts, depth, quoted, start, i = [], 0, False, 0, 0
    while i < len(text):
        char = text[i]
        if char == '"':
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif depth == 0 and text.startswith(" and ", i):
                parts.append(text[start:i])
                start = i + len(" and ")
                i = start - 1
        i += 1
    parts.append(text[start:])
    return parts


def _unwrap(text: str) -> str:
    """Remove parentheses that enclose the whole text, however many layers."""
    while text.startswith("(") and text.endswith(")"):
        depth, quoted = 0, False
        for i, char in enumerate(text):
            if char == '"':
                quoted = not quoted
            elif not quoted:
                depth += char == "("
                depth -= char == ")"
                if depth == 0 and i < len(text) - 1:
                    return text  # the first parenthesis closes before the end
        text = text[1:-1].strip()
    return text


def conjuncts(line: str) -> list[str]:
    """The normalized top-level `and` parts of a line, outer parentheses removed."""
    return [_unwrap(part.strip()) for part in _top_level_parts(normalize(line))]


def _patterns(kind: str, lines: Sequence[str]) -> set[str]:
    if kind == "command":
        return actiontemplate.commands(list(lines))
    if not lines:
        return set()
    if kind == "line":
        found = {normalize(line) for line in lines}
    elif kind == "conjunct":
        found = {part for line in lines for part in conjuncts(line)}
    elif kind == "opening":
        found = {normalize(lines[0])}
    else:
        found = {shape(line) for line in lines}
    return {pattern for pattern in found if pattern}


def _count(
    units: Sequence[_Unit], kinds: Sequence[str]
) -> list[tuple[str, Counter, dict[str, list[str]]]]:
    """Per kind: how many distinct items hold each pattern, and which items (first few)."""
    out = []
    for kind in kinds:
        counts: Counter = Counter()
        examples: dict[str, list[str]] = {}
        for unit in units:
            for pattern in _patterns(kind, unit.lines):
                counts[pattern] += 1
                holders = examples.setdefault(pattern, [])
                if len(holders) < EXAMPLES:
                    holders.append(unit.content_id)
        out.append((kind, counts, examples))
    return out


def _units(clauses: Sequence[ClauseRow], groups: Mapping[str, str]) -> list[_Unit]:
    units = []
    for clause in clauses:
        group = groups.get(clause.source_id, clause.source_id)
        units.append(
            _Unit(group, clause.role, clause.platform, clause.content_id, clause.relevance)
        )
    return units


def mine(
    clauses: Sequence[ClauseRow],
    groups: Mapping[str, str],
    *,
    min_files: int = MIN_FILES,
    min_share: float | Mapping[str, float] | None = None,
    top: int = TOP_PER_KIND,
) -> list[ConventionRow]:
    """Count patterns per (group, role, platform) and keep those with enough support.

    min_share is one number for every role, a number per role, or None for the defaults.
    """
    shares = resolve_min_share(min_share)
    units = _units(clauses, groups)
    # one item counts once however many sources or copies hold it
    cells: dict[tuple[str, str, str], dict[str, _Unit]] = {}
    for unit in units:
        for platform in (unit.platform, PLATFORM_ALL):
            cell = cells.setdefault((unit.group, unit.role, platform), {})
            cell.setdefault(unit.content_id, unit)
    rows: list[ConventionRow] = []
    for (group, role, platform), members in sorted(cells.items()):
        total = len(members)
        for kind, counts, examples in _count(list(members.values()), KINDS_BY_ROLE[role]):
            kept = [
                (pattern, files)
                for pattern, files in counts.items()
                if files >= min_files and files / total >= shares[role]
            ]
            kept.sort(key=lambda pair: (-pair[1], pair[0]))
            rows += [
                ConventionRow(
                    source_group=group,
                    role=role,
                    platform=platform,
                    kind=kind,
                    pattern=pattern[:PATTERN_CHARS],
                    files=files,
                    total=total,
                    share=round(files / total, 4),
                    examples=tuple(examples[pattern]),
                )
                for pattern, files in kept[:top]
            ]
    return rows

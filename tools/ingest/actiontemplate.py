"""Turn an action script into a template, its relevance substitutions and its commands.

An action script is mostly commands, but most scripts also carry relevance written in
curly braces, evaluated on the client when the action runs:

    waithidden msiexec.exe /X {name of keys whose (...) of keys "..." of native registry} /qn

A template keeps the script as written but replaces each substitution with the generic
marker `{<relevance>}`, so scripts that differ only in what they substitute look alike,
and the substitutions are returned separately so the analyzer can check each one.

Brace rules, from the vendor's action script guide ("Using Substitution"):

  {expr}   a substitution. It ends at the first single `}`, even inside a string.
  {}       with nothing inside, plain text: there is no relevance to evaluate.
  {{       outside a substitution, one literal `{`. This is the only escape there: a
           single `}` outside a substitution is already a literal `}`, and `}}` outside
           one is two ordinary characters, not an escape.
  }}       inside a substitution only, one literal `}`, so that a string such as
           "\\:{ScanTrigger:OnAccess}}" does not end the substitution early. A `{` inside a
           substitution is an ordinary character.

Writing `}}` for a closing brace in a script (as in a PowerShell block) therefore puts
two braces in the file; double only the left braces. The template keeps every escape as
written, so a copy stays valid.

A substitution that never closes makes the script *ambiguous*: the template is the
script unchanged, no substitutions are reported, and nothing is guessed.

`createfile` and `appendfile` blocks (`createfile until MARKER` ... `MARKER`) hold the
text of another file, not commands. The full template keeps them, because a PowerShell or
XML body is part of what an author wants to see, but `command_lines` collapses each to a
single `<file content>` line so that body text is never mistaken for a habit.

This module reads text and returns text. It runs nothing.
"""

import re
from dataclasses import dataclass

PLACEHOLDER = "{<relevance>}"
FILE_CONTENT = "<file content>"

_BLOCK_START = re.compile(r"^\s*(?:createfile|appendfile)\s+until\s+(\S+)", re.IGNORECASE)
_PARAMETER_LINE = re.compile(r'^\s*parameter\s+"([^"]+)"', re.IGNORECASE | re.MULTILINE)
_PARAMETER_USE = re.compile(r'\bparameter\s+"([^"]+)"', re.IGNORECASE)
_COMMAND = re.compile(r"^[a-z_][a-z0-9_]*")


@dataclass(frozen=True)
class Template:
    text: str  # the script with each substitution replaced by PLACEHOLDER
    substitutions: tuple[str, ...]  # the relevance of each, in order
    parameters: tuple[str, ...]  # parameter names declared or used, sorted
    ambiguous: bool = False  # a substitution never closed; nothing was extracted


def _parameters(script: str, substitutions: tuple[str, ...]) -> tuple[str, ...]:
    names = set(_PARAMETER_LINE.findall(script))
    for expression in substitutions:
        names.update(_PARAMETER_USE.findall(expression))
    return tuple(sorted(names))


def parse(script: str) -> Template:
    """Split a script into its template and substitutions."""
    out: list[str] = []
    expressions: list[str] = []
    i, size = 0, len(script)
    while i < size:
        char = script[i]
        if char == "{" and script.startswith("{{", i):
            out.append("{{")
            i += 2
        elif char == "{":
            expression: list[str] = []
            j = i + 1
            closed = False
            while j < size:
                if script[j] == "}":
                    if script.startswith("}}", j):
                        expression.append("}")
                        j += 2
                        continue
                    closed = True
                    break
                expression.append(script[j])
                j += 1
            if not closed:
                return Template(script, (), _parameters(script, ()), ambiguous=True)
            text = "".join(expression).strip()
            if text:
                expressions.append(text)
                out.append(PLACEHOLDER)
            else:
                # `{}` has nothing to evaluate (a shell's own `{}`, say): it is text
                out.append(script[i : j + 1])
            i = j + 1
        else:
            out.append(char)
            i += 1
    substitutions = tuple(expressions)
    return Template("".join(out), substitutions, _parameters(script, substitutions))


def command_lines(template: str) -> list[str]:
    """The script's own lines: blank lines dropped, each file block collapsed to one line."""
    lines: list[str] = []
    marker = ""
    for raw in template.splitlines():
        line = raw.strip()
        if marker:
            if line == marker:
                marker = ""
            continue
        if not line:
            continue
        lines.append(line)
        start = _BLOCK_START.match(line)
        if start:
            marker = start.group(1)
            lines.append(FILE_CONTENT)
    return lines


def commands(lines: list[str]) -> set[str]:
    """The command words that open each line (comments and file content are not commands)."""
    found = set()
    for line in lines:
        if line.startswith(("//", "<", "@")):
            continue
        match = _COMMAND.match(line.lower())
        if match:
            found.add(match.group(0))
    return found

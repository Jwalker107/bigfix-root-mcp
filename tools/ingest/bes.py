"""Read the relevance out of BigFix .bes files.

Shared by the question collector (tools/eval/questions.py) and the reference
fixlet index, so there is one parser and one set of safety rules.

  applicability  a Fixlet or Task: all its <Relevance> lines together decide which
                 computers it applies to. One statement per line, role 'applicability'.
  property       an Analysis <Property>: relevance that reports a value. One statement
                 per property, role 'property', carrying the property's name.

Safety, because these files may be confidential and are never trusted:

  - the XML parser resolves no entities and loads no DTD or network resource, so a
    hostile file cannot make this read anything else;
  - nothing under the source directory is written or modified;
  - a symbolic link or junction is never followed, for a file or a directory, so a
    link cannot lead the walk outside the directory it was given;
  - one file that is not valid XML is recorded and skipped, never fatal.

Action scripts (the <DefaultAction> and <Action> of a Fixlet or Task) are read only
when asked for (actions=True), because they are where credentials and internal
hostnames most often sit; the question collector never asks.
"""

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from tools.ingest import convert

DESCRIPTION_CHARS = 600
# how much of one action script is kept as purpose text in a search hit
ACTION_CHARS = 1500
# the most of one action script that is read at all; a longer one is cut and flagged
SCRIPT_CHARS = 64 * 1024
# directories that hold tooling or dependencies, never content worth reading
SKIP_DIRS = frozenset({".git", "node_modules", "site-packages", "__pycache__", ".venv", "venv"})
KINDS = ("Fixlet", "Task", "Analysis")
EXTENSION = ".bes"


@dataclass
class Statement:
    role: str  # 'applicability' or 'property'
    relevance: str
    name: str = ""  # a property's name


@dataclass
class Action:
    """One <DefaultAction> or <Action> of a Fixlet or Task."""

    kind: str  # 'default' or 'action'
    action_id: str
    mime_type: str
    script: str  # the action script, cut at SCRIPT_CHARS
    truncated: bool = False
    # the SuccessCriteria option: OriginalRelevance, CustomRelevance, ... or '' when absent
    success_criteria: str = ""


@dataclass
class Item:
    kind: str  # Fixlet, Task or Analysis
    title: str
    description: str
    path: str  # relative to the source directory, posix separators
    statements: list[Statement] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)  # only if asked for


@dataclass
class Extraction:
    items: list[Item]
    files: int = 0
    unparsable: list[str] = field(default_factory=list)
    duplicate_files: int = 0
    duplicate_items: int = 0
    # SHA-256 over every file read (relative path and content), so one tree always gives
    # one value and any added, removed, renamed or edited file changes it
    fingerprint: str = ""


def _parser() -> etree.XMLParser:
    # no entity expansion, no DTD, no network: a content file must never read anything else
    return etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, recover=False
    )


def _text(element: etree._Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _description(element: etree._Element | None) -> str:
    """A description as plain text: it is often HTML, escaped or in a CDATA section."""
    raw = _text(element)
    if "<" in raw:
        raw = convert.html_to_markdown(raw)
    return re.sub(r"\s+", " ", raw).strip()[:DESCRIPTION_CHARS]


def content_id(kind: str, title: str, role: str, statements: list[Statement]) -> str:
    """A short id from what an item says, not where it lives.

    The same item in two places (or two checkouts) gets the same id, which is what lets
    an evaluation hold out the item a question was written from.
    """
    parts = (kind, title, role, *(s.name + s.relevance for s in statements))
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def item_id(item: Item) -> str:
    """A short id for the item as a whole, shared by its relevance and its actions.

    Built from the kind, the title and the applicability lines, so an analysis's
    properties and a fixlet's actions all carry the id of the item they belong to. That
    is what lets an evaluation hold out everything about the item a question came from.
    """
    lines = [s.relevance for s in item.statements if s.role == "applicability"]
    parts = (item.kind, item.title, *lines)
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def _actions(element: etree._Element) -> list[Action]:
    """The default action first, then the others, each with a non-empty script."""
    found = []
    for tag, kind in (("DefaultAction", "default"), ("Action", "action")):
        for action in element.findall(tag):
            script = action.find("ActionScript")
            text = _text(script)
            if script is None or not text:
                continue
            criteria = action.find("SuccessCriteria")
            found.append(
                Action(
                    kind=kind,
                    action_id=_text(action.find("ID")),
                    mime_type=script.get("MIMEType", ""),
                    script=text[:SCRIPT_CHARS],
                    truncated=len(text) > SCRIPT_CHARS,
                    success_criteria="" if criteria is None else criteria.get("Option", ""),
                )
            )
    return found


def parse_bes(data: bytes, path: str, *, actions: bool = False) -> list[Item]:
    """Read the items in one BES document. Raises etree.XMLSyntaxError if it is not XML."""
    root = etree.fromstring(data, _parser())
    items = []
    for element in root:
        kind = etree.QName(element).localname if isinstance(element.tag, str) else ""
        if kind not in KINDS:
            continue
        item = Item(
            kind=kind,
            title=_text(element.find("Title")),
            description=_description(element.find("Description")),
            path=path,
        )
        item.statements = [
            Statement("applicability", _text(relevance))
            for relevance in element.findall("Relevance")
            if _text(relevance)
        ]
        if kind == "Analysis":
            item.statements += [
                Statement("property", _text(prop), name=prop.get("Name", ""))
                for prop in element.findall("Property")
                if _text(prop)
            ]
        if actions and kind != "Analysis":
            item.actions = _actions(element)
        if item.statements:
            items.append(item)
    return items


def _is_link(path: Path) -> bool:
    """A symbolic link, or on Windows a junction, which is_symlink() does not report."""
    return path.is_symlink() or getattr(os.path, "isjunction", lambda _p: False)(path)


def bes_files(root: Path) -> list[Path]:
    """Every .bes file under root, in any letter case (.BES too), in a stable order.

    The order is by relative posix path, so one tree always gives one sequence on any
    platform. Symbolic links and the directories in SKIP_DIRS are left alone.
    """
    found: list[tuple[str, Path]] = []
    for directory, subdirs, names in os.walk(root, followlinks=False):
        here = Path(directory)
        # prune in place: os.walk does not descend into what is removed from subdirs
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS and not _is_link(here / d)]
        for name in names:
            path = here / name
            if path.suffix.lower() == EXTENSION and not _is_link(path):
                found.append((path.relative_to(root).as_posix(), path))
    return [path for _, path in sorted(found, key=lambda pair: pair[0])]


def extract(root: Path, *, actions: bool = False) -> Extraction:
    """Read every .bes file under root; identical files and identical items count once."""
    result = Extraction(items=[])
    seen_files: set[str] = set()
    seen_items: set[tuple] = set()
    entries: list[str] = []
    for path in bes_files(root):
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        result.files += 1
        entries.append(f"{relative}\0{digest}\n")
        if digest in seen_files:
            result.duplicate_files += 1
            continue
        seen_files.add(digest)
        try:
            items = parse_bes(data, relative, actions=actions)
        except etree.XMLSyntaxError:
            result.unparsable.append(relative)
            continue
        for item in items:
            key = (
                item.kind,
                item.title,
                tuple((s.role, s.name, s.relevance) for s in item.statements),
            )
            if key in seen_items:
                result.duplicate_items += 1
                continue
            seen_items.add(key)
            result.items.append(item)
    result.fingerprint = hashlib.sha256("".join(entries).encode("utf-8")).hexdigest()
    return result

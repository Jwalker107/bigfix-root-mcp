"""Optional house conventions for the write_relevance prompt.

A team's relevance habits (how to treat proxy agents, which guard idioms to use,
naming) are not in any public documentation, and they differ between teams, so
they do not belong in the shipped prompt. BIGFIX_RELEVANCE_CONVENTIONS names a
markdown file; when it is set, its text is added to the prompt as instructions to
follow unless the user says otherwise.

The path comes from the server's own configuration, never from a caller, so a
client cannot point the server at an arbitrary file. The file is read on every
call (an edit takes effect without a restart), only as UTF-8 text, and capped in
size so a wrong path cannot flood a prompt. Unset means no conventions; set but
unreadable is reported in the prompt instead of being ignored, because a rule
that silently stopped applying is worse than a visible problem.

HTML comments (`<!-- ... -->`) are for the people editing the file, such as a note saying
the file is an editable default; they are removed before the text is sent.

This module must not import fastmcp (same layering rule as analysis.py).
"""

import os
import re
from pathlib import Path

CONVENTIONS_ENV = "BIGFIX_RELEVANCE_CONVENTIONS"
# a conventions file is a page of rules, not a document; refuse to read more
MAX_FILE_BYTES = 64 * 1024
# what reaches the prompt; the rest is cut and the cut is said out loud
MAX_PROMPT_CHARS = 8000
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BLANK_RUNS = re.compile(r"\n{3,}")


def load() -> tuple[str, str]:
    """Return (text, problem): the conventions, or why they could not be read.

    Both are empty when nothing is configured. `problem` is a short sentence for
    the prompt; it never includes file contents.
    """
    raw = os.environ.get(CONVENTIONS_ENV, "").strip()
    if not raw:
        return "", ""
    path = Path(raw).expanduser()
    if not path.is_file():
        return "", f"{CONVENTIONS_ENV} is set but there is no file at {path}"
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            return "", f"the conventions file {path} is {size} bytes; the limit is {MAX_FILE_BYTES}"
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        return "", f"the conventions file {path} could not be read ({type(err).__name__})"
    text = _BLANK_RUNS.sub("\n\n", _COMMENT.sub("", text)).strip()
    if len(text) > MAX_PROMPT_CHARS:
        text = (
            text[:MAX_PROMPT_CHARS].rstrip()
            + "\n\n[conventions cut here: file longer than the limit]"
        )
    return text, ""


def section() -> str:
    """The prompt section for the configured conventions, or "" when there is nothing to say."""
    text, problem = load()
    if problem:
        return (
            "\n\nHouse conventions: a conventions file is configured but could not be used "
            f"({problem}). Say so in your answer instead of guessing what it contained."
        )
    if not text:
        return ""
    return (
        "\n\nHouse conventions, set by whoever configured this server. They are instructions: "
        "follow them unless the user says otherwise, and say where one shaped your answer.\n\n"
        f"{text}"
    )

"""The first-party relevance libraries are hard dependencies with bounds.

Each one floors at the release this server was built against and stops
before the next major, where a breaking change would be allowed.
"""

import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

EXPECTED = {
    "bigfix-relevance-analyzer": ">=1.38.1,<2",
    "bigfix-remote-session-relevance": ">=0.5.0,<1",
    "bigfix-remote-client-relevance": ">=1.5.4,<2",
}


def _dependencies() -> dict[str, str]:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    specs = {}
    for requirement in project["dependencies"]:
        name = requirement.split(">", 1)[0].split("=", 1)[0].split("<", 1)[0].strip()
        specs[name] = requirement[len(name) :].strip()
    return specs


@pytest.mark.parametrize(("name", "spec"), EXPECTED.items())
def test_required_with_bounds(name, spec):
    assert _dependencies().get(name) == spec

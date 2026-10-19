"""The first-party relevance libraries are hard dependencies with bounds.

Each one floors at the release this server was built against and stops
before the next major, where a breaking change would be allowed (for
bigfix-remote-session-relevance that is 2, since 1.x keeps the 0.5 contract).
"""

import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

EXPECTED = {
    "bigfix-relevance-analyzer": ">=1.38.1,<2",
    # 1.x is announced as contract-compatible with 0.5, so the cap is the
    # next major after it rather than the next one after 0.x
    "bigfix-remote-session-relevance": ">=0.5.2,<2",
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


FIRST_PARTY = sorted(name for name in EXPECTED if name.startswith("bigfix-"))


def test_first_party_dependencies_have_no_uv_cooldown():
    """jgstew's own bigfix-* packages iterate faster than any cooldown."""
    uv = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["uv"]
    # every bigfix-* base dependency is listed, so a new one cannot be missed
    declared = sorted(name for name in _dependencies() if name.startswith("bigfix-"))
    assert declared == FIRST_PARTY
    assert uv["exclude-newer-package"] == dict.fromkeys(FIRST_PARTY, False)


def test_first_party_dependencies_have_no_dependabot_cooldown():
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load((PYPROJECT.parent / ".github" / "dependabot.yml").read_text())
    (uv,) = (u for u in config["updates"] if u["package-ecosystem"] == "uv")
    assert sorted(uv["cooldown"]["exclude"]) == FIRST_PARTY

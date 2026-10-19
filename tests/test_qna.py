"""Gated qna evaluation surface (bigfix-remote-client-relevance).

The registration gate is the security boundary (same design as writes), so the
tests that matter most are which tools exist and which targets are refused.
The package itself is faked at the sys.modules seam: these tests need no
docker and no ssh. TestRealPackageContract checks the fake against the real
installed package.
"""

import importlib
import sys
import types
from dataclasses import dataclass, field

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from bigfix_root_mcp import qna, server

QNA_TOOLS = {"evaluate_client_relevance_qna", "list_qna_targets"}
PKG = "bigfix_remote_client_relevance"


def make_fake_package():
    """A scriptable stand-in for bigfix_remote_client_relevance."""
    fake = types.ModuleType(PKG)

    @dataclass
    class Target:
        kind: str
        name: str
        user: str | None = None
        become: bool | None = None
        image: str | None = None
        qna_version: str | None = None

        @property
        def label(self):
            return f"container:{self.image}" if self.kind == "container" else self.name

    @dataclass
    class FakeResult:
        host: str
        answers: list = field(default_factory=list)
        error: str | None = None
        error_kind: str | None = None
        elapsed_ms: int = 5

    fake.Target = Target
    fake.FakeResult = FakeResult
    fake.inventory = []  # what load_inventory returns
    fake.scripted_results = []  # what the stream yields
    fake.calls = []  # (name, ...) tuples for assertions

    def load_inventory(path):
        fake.calls.append(("load_inventory", path))
        return list(fake.inventory)

    def count_work(targets, qna_version=None):
        return len(targets)

    def result_to_dict(result, *, max_raw_output=None):
        return {
            "host": result.host,
            "answers": list(result.answers),
            "error": result.error,
            "error_kind": result.error_kind,
            "elapsed_ms": result.elapsed_ms,
        }

    async def evaluate_client_relevance_stream(relevance, targets, **kwargs):
        fake.calls.append(("evaluate", relevance, tuple(t.label for t in targets), kwargs))
        for result in fake.scripted_results:
            yield result

    fake.load_inventory = load_inventory
    fake.count_work = count_work
    fake.result_to_dict = result_to_dict
    fake.evaluate_client_relevance_stream = evaluate_client_relevance_stream
    return fake


@pytest.fixture
def fake_pkg(monkeypatch):
    fake = make_fake_package()
    monkeypatch.setitem(sys.modules, PKG, fake)
    return fake


@pytest.fixture
def gated(fake_pkg):
    """A server module reloaded with the fake package importable (gate on)."""
    reloaded = importlib.reload(server)
    yield reloaded
    # the fake must be gone *before* the restoring reload, or the gate stays on
    del sys.modules[PKG]
    importlib.reload(server)


async def call(mcp_server, tool, arguments=None):
    async with Client(mcp_server.mcp) as client:
        return await client.call_tool(tool, arguments or {})


class TestResolveTargets:
    """Target policy: inventory names + container images, nothing else."""

    def test_container_images_become_container_targets(self, fake_pkg):
        targets = qna.resolve_targets(None, ["ubuntu:22.04", "debian:12"])
        assert [t.kind for t in targets] == ["container", "container"]
        assert [t.image for t in targets] == ["ubuntu:22.04", "debian:12"]

    def test_hostile_image_string_is_refused(self, fake_pkg):
        with pytest.raises(ValueError, match="image"):
            qna.resolve_targets(None, ["ubuntu; rm -rf /"])

    def test_inventory_names_resolve_from_the_admin_file(self, fake_pkg, monkeypatch):
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", "/etc/qna-hosts.toml")
        fake_pkg.inventory = [
            fake_pkg.Target(kind="ssh", name="mac-test", become=True),
            fake_pkg.Target(kind="local", name="local"),
        ]
        targets = qna.resolve_targets(["mac-test"], None)
        assert len(targets) == 1
        # the admin's target is passed through untouched (become stays theirs)
        assert targets[0].become is True

    def test_unknown_inventory_name_is_refused(self, fake_pkg, monkeypatch):
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", "/etc/qna-hosts.toml")
        fake_pkg.inventory = [fake_pkg.Target(kind="local", name="local")]
        with pytest.raises(ValueError, match="mac-test"):
            qna.resolve_targets(["mac-test"], None)

    def test_inventory_names_without_configured_file_are_refused(self, fake_pkg, monkeypatch):
        monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
        with pytest.raises(ValueError, match="BIGFIX_QNA_INVENTORY"):
            qna.resolve_targets(["mac-test"], None)

    def test_images_refused_when_containers_disabled(self, fake_pkg, monkeypatch):
        monkeypatch.setenv("BIGFIX_QNA_CONTAINERS", "0")
        with pytest.raises(ValueError, match="container"):
            qna.resolve_targets(None, ["ubuntu:22.04"])

    def test_zero_targets_is_refused(self, fake_pkg):
        with pytest.raises(ValueError, match="target"):
            qna.resolve_targets(None, None)

    def test_too_many_targets_is_refused(self, fake_pkg):
        images = [f"img{i}:latest" for i in range(qna.MAX_QNA_TARGETS + 1)]
        with pytest.raises(ValueError, match=str(qna.MAX_QNA_TARGETS)):
            qna.resolve_targets(None, images)


@pytest.fixture
def ungated(monkeypatch):
    """A server module reloaded with every qna target source disabled.

    The gate must be forced off rather than assumed off: the package is a
    base dependency, so it is always importable.
    """
    monkeypatch.setenv("BIGFIX_QNA_CONTAINERS", "0")
    monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
    reloaded = importlib.reload(server)
    yield reloaded
    monkeypatch.undo()
    importlib.reload(server)


class TestQnaGate:
    async def test_qna_tools_absent_when_gate_off(self, ungated):
        """Paired with the gate-on test below: alone this would pass trivially."""
        async with Client(ungated.mcp) as client:
            tools = {tool.name for tool in await client.list_tools()}
        assert not (tools & QNA_TOOLS)

    async def test_qna_tools_present_when_package_importable(self, gated):
        async with Client(gated.mcp) as client:
            tools = {tool.name for tool in await client.list_tools()}
        assert QNA_TOOLS <= tools

    async def test_gate_off_when_all_target_sources_disabled(self, fake_pkg, monkeypatch):
        monkeypatch.setenv("BIGFIX_QNA_CONTAINERS", "0")
        monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
        assert qna.qna_enabled() is False

    async def test_whoami_reports_the_gate_state(self, ungated, fake_conn):
        async with Client(ungated.mcp) as client:
            result = await client.call_tool("whoami", {})
        assert result.data["qna_enabled"] is False


class TestEvaluateClientRelevanceQna:
    """Runs against a server module reloaded with the fake package importable."""

    async def test_happy_path_shapes_results(self, gated, fake_pkg):
        fake_pkg.scripted_results = [
            fake_pkg.FakeResult(host="container:ubuntu:22.04", answers=["Linux Ubuntu 22.04"]),
            fake_pkg.FakeResult(
                host="container:debian:12",
                error="qna exited 2",
                error_kind="qna",
            ),
        ]
        result = await call(
            gated,
            "evaluate_client_relevance_qna",
            {
                "relevance": "name of operating system",
                "container_images": ["ubuntu:22.04", "debian:12"],
            },
        )
        assert result.data["target_count"] == 2
        assert result.data["ok_count"] == 1
        # a per-target failure is a result row, never a tool error
        errors = [r["error_kind"] for r in result.data["results"]]
        assert errors == [None, "qna"]

    async def test_bad_target_refused_before_any_evaluation(self, gated, fake_pkg):
        with pytest.raises(ToolError, match="image"):
            await call(
                gated,
                "evaluate_client_relevance_qna",
                {"relevance": "true", "container_images": ["ubuntu; rm -rf /"]},
            )
        assert not [c for c in fake_pkg.calls if c[0] == "evaluate"]

    async def test_timeout_is_clamped(self, gated, fake_pkg):
        fake_pkg.scripted_results = [fake_pkg.FakeResult(host="container:ubuntu:22.04")]
        await call(
            gated,
            "evaluate_client_relevance_qna",
            {
                "relevance": "true",
                "container_images": ["ubuntu:22.04"],
                "timeout_seconds": 99999,
            },
        )
        evaluate = next(c for c in fake_pkg.calls if c[0] == "evaluate")
        assert evaluate[3]["timeout_s"] == qna.MAX_QNA_TIMEOUT_SECONDS

    async def test_qna_version_passes_through(self, gated, fake_pkg):
        """A plain container image has no qna baked in; the version must be
        forwardable or container targets can never provision one."""
        fake_pkg.scripted_results = [fake_pkg.FakeResult(host="container:ubuntu:22.04")]
        await call(
            gated,
            "evaluate_client_relevance_qna",
            {
                "relevance": "true",
                "container_images": ["ubuntu:22.04"],
                "qna_version": "11.0",
            },
        )
        evaluate = next(c for c in fake_pkg.calls if c[0] == "evaluate")
        assert evaluate[3]["qna_version"] == "11.0"

    async def test_flagged_relevance_carries_advisory(self, gated, fake_pkg):
        fake_pkg.scripted_results = [fake_pkg.FakeResult(host="container:ubuntu:22.04")]
        result = await call(
            gated,
            "evaluate_client_relevance_qna",
            {
                "relevance": "namez of operating systemz",
                "container_images": ["ubuntu:22.04"],
            },
        )
        assert result.data["analysis"]["findings"]

    async def test_every_run_is_audit_logged(self, gated, fake_pkg, caplog):
        fake_pkg.scripted_results = [fake_pkg.FakeResult(host="container:ubuntu:22.04")]
        with caplog.at_level("INFO"):
            await call(
                gated,
                "evaluate_client_relevance_qna",
                {"relevance": "true", "container_images": ["ubuntu:22.04"]},
            )
        audit = [r for r in caplog.records if "BIGFIX QNA" in r.getMessage()]
        assert audit, "every qna run must leave an audit line"

    async def test_list_qna_targets_reports_inventory_and_policy(
        self, gated, fake_pkg, monkeypatch
    ):
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", "/etc/qna-hosts.toml")
        fake_pkg.inventory = [fake_pkg.Target(kind="ssh", name="mac-test")]
        result = await call(gated, "list_qna_targets")
        assert result.data["containers_allowed"] is True
        assert result.data["inventory"] == [
            {"name": "mac-test", "kind": "ssh", "label": "mac-test"}
        ]


class TestRealPackageContract:
    """bigfix-remote-client-relevance is a base dependency now, so the fake
    above must keep matching the real API this module calls."""

    @pytest.fixture(autouse=True)
    def real_pkg(self, monkeypatch):
        monkeypatch.delitem(sys.modules, PKG, raising=False)
        return importlib.import_module(PKG)

    def test_installed_by_default(self):
        assert qna.package_available() is True

    def test_tools_registered_with_default_environment(self, monkeypatch):
        monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
        monkeypatch.delenv("BIGFIX_QNA_CONTAINERS", raising=False)
        assert qna.qna_enabled() is True

    def test_calls_match_the_real_signatures(self, real_pkg):
        import inspect

        target = real_pkg.Target(kind="container", name="ubuntu:22.04", image="ubuntu:22.04")
        assert target.label  # list_inventory_targets and the audit log read it
        # exactly as qna.py / server.py call them
        inspect.signature(real_pkg.load_inventory).bind("/etc/qna-hosts.toml")
        inspect.signature(real_pkg.count_work).bind([target], None)
        inspect.signature(real_pkg.evaluate_client_relevance_stream).bind(
            "computer name",
            [target],
            qna_version=None,
            max_parallel=1,
            timeout_s=30,
        )
        inspect.signature(real_pkg.result_to_dict).bind(object(), max_raw_output=4000)

    def test_container_targets_build_with_the_real_target(self):
        targets = qna.resolve_targets(None, ["ubuntu:22.04"])
        assert targets[0].kind == "container"
        assert targets[0].image == "ubuntu:22.04"

    def test_result_payload_has_the_keys_the_server_reads(self, real_pkg):
        # server.py reads error_kind from each shaped result for progress
        assert "error_kind" in real_pkg.ResultPayload.__annotations__

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
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from bigfix_root_mcp import qna, server

QNA_TOOLS = {"qna_eval_client_relevance", "qna_list_targets", "qna_discover_targets"}
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

    class InventoryError(Exception):
        pass

    fake.Target = Target
    fake.FakeResult = FakeResult
    fake.InventoryError = InventoryError
    fake.inventory = []  # what load_inventory returns
    fake.inventory_error = None  # raised by load_inventory instead, when set
    fake.scripted_results = []  # what the stream yields
    fake.calls = []  # (name, ...) tuples for assertions

    def load_inventory(path):
        fake.calls.append(("load_inventory", path))
        if fake.inventory_error is not None:
            raise fake.inventory_error
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


USER_INVENTORY = "/home/tester/.bigfix/remote_clients.toml"


@pytest.fixture
def fake_pkg(monkeypatch):
    fake = make_fake_package()
    monkeypatch.setitem(sys.modules, PKG, fake)
    # the default search and auto-discovery live in the package's submodules,
    # which a plain fake module has none of; script them at qna's own seams
    fake.default_inventory = None  # what the default search finds
    fake.discovered = []  # inventory auto-discovery "writes"

    def find_default_inventory():
        fake.calls.append(("find_default_inventory",))
        return fake.default_inventory

    def run_auto_discovery(path):
        fake.calls.append(("run_auto_discovery", path))
        fake.inventory = list(fake.discovered)
        fake.inventory_error = None
        fake.default_inventory = path
        return [target.name for target in fake.discovered]

    monkeypatch.setattr(qna, "_find_default_inventory", find_default_inventory)
    monkeypatch.setattr(qna, "_run_auto_discovery", run_auto_discovery)
    monkeypatch.setattr(qna, "_user_inventory_path", lambda: USER_INVENTORY)
    return fake


def called(fake, name):
    return [c for c in fake.calls if c[0] == name]


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

    def test_no_selection_means_every_inventory_host(self, fake_pkg, monkeypatch):
        """qna is read-only, so like the CLI an unselected run covers the fleet."""
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", "/etc/qna-hosts.toml")
        fake_pkg.inventory = [
            fake_pkg.Target(kind="ssh", name="mac-test"),
            fake_pkg.Target(kind="local", name="local"),
        ]
        targets = qna.resolve_targets(None, None)
        assert [t.name for t in targets] == ["mac-test", "local"]

    def test_no_selection_over_the_cap_is_refused(self, fake_pkg, monkeypatch):
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", "/etc/qna-hosts.toml")
        fake_pkg.inventory = [
            fake_pkg.Target(kind="ssh", name=f"h{i}") for i in range(qna.MAX_QNA_TARGETS + 1)
        ]
        with pytest.raises(ValueError, match="inventory_targets"):
            qna.resolve_targets(None, None)

    def test_explicit_containers_do_not_pull_in_the_inventory(self, fake_pkg, monkeypatch):
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", "/etc/qna-hosts.toml")
        fake_pkg.inventory = [fake_pkg.Target(kind="ssh", name="mac-test")]
        targets = qna.resolve_targets(None, ["ubuntu:22.04"])
        assert [t.kind for t in targets] == ["container"]

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
    monkeypatch.setenv("BIGFIX_QNA_INVENTORY", "0")
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
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", "0")
        assert qna.qna_enabled() is False

    async def test_gate_on_with_only_the_default_inventory(self, fake_pkg, monkeypatch):
        """Unset means the default search (and discovery), which is a source."""
        monkeypatch.setenv("BIGFIX_QNA_CONTAINERS", "0")
        monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
        assert qna.qna_enabled() is True
        # the gate is read at import; it must not search or probe anything
        assert not called(fake_pkg, "find_default_inventory")

    async def test_whoami_reports_the_gate_state(self, ungated, fake_conn):
        async with Client(ungated.mcp) as client:
            result = await client.call_tool("whoami", {})
        assert result.data["qna_enabled"] is False


class TestInventoryPath:
    """BIGFIX_QNA_INVENTORY wins; unset falls back to the CLI's default search."""

    def test_configured_path_wins(self, fake_pkg, monkeypatch):
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", "/etc/qna-hosts.toml")
        fake_pkg.default_inventory = USER_INVENTORY
        assert qna.inventory_path() == "/etc/qna-hosts.toml"
        assert not called(fake_pkg, "find_default_inventory")

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_unset_or_blank_uses_the_default_search(self, fake_pkg, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
        else:
            monkeypatch.setenv("BIGFIX_QNA_INVENTORY", value)
        fake_pkg.default_inventory = USER_INVENTORY
        assert qna.inventory_path() == USER_INVENTORY

    def test_default_search_finding_nothing_is_no_inventory(self, fake_pkg, monkeypatch):
        monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
        assert qna.inventory_path() == ""
        assert qna.list_inventory_targets() == []

    @pytest.mark.parametrize("value", ["0", "off", "false", "no", "NO"])
    def test_off_values_disable_the_inventory(self, fake_pkg, monkeypatch, value):
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", value)
        fake_pkg.default_inventory = USER_INVENTORY
        assert qna.inventory_path() == ""
        assert not qna.discovery_allowed()
        assert not called(fake_pkg, "find_default_inventory")

    def test_an_inventory_with_no_hosts_has_no_targets(self, fake_pkg, monkeypatch, tmp_path):
        """Discovery leaves a comment-only placeholder when it finds nothing;
        the package refuses to load it, but here it just means no hosts."""
        placeholder = tmp_path / "remote_clients.toml"
        placeholder.write_text("# no hosts found yet\n")
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", str(placeholder))
        fake_pkg.inventory_error = fake_pkg.InventoryError("defines no [hosts.*] entries")
        assert qna.list_inventory_targets() == []

    def test_a_broken_inventory_still_fails(self, fake_pkg, monkeypatch, tmp_path):
        broken = tmp_path / "remote_clients.toml"
        broken.write_text("[hosts.a]\ntransport = 'ssh'\n[[[\n")
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", str(broken))
        fake_pkg.inventory_error = fake_pkg.InventoryError("could not parse")
        with pytest.raises(fake_pkg.InventoryError):
            qna.list_inventory_targets()


class TestAutoDiscovery:
    """Discovery runs by itself only when no inventory file exists (the CLI's
    first run); with a file, only when the caller asks for it."""

    def test_allowed_by_default(self, fake_pkg, monkeypatch):
        monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
        monkeypatch.delenv("BIGFIX_QNA_AUTO_DISCOVERY", raising=False)
        assert qna.discovery_allowed() is True

    def test_switched_off(self, fake_pkg, monkeypatch):
        monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
        monkeypatch.setenv("BIGFIX_QNA_AUTO_DISCOVERY", "0")
        assert qna.discovery_allowed() is False

    def test_never_with_an_admin_pinned_inventory(self, fake_pkg, monkeypatch):
        """Discovery writes ~/.bigfix; a pinned file is the admin's, not ours to grow."""
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", "/etc/qna-hosts.toml")
        assert qna.discovery_allowed() is False
        assert qna.discovery_needed() is False

    def test_needed_when_no_inventory_exists(self, fake_pkg, monkeypatch):
        monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
        assert qna.discovery_needed() is True

    def test_not_needed_when_the_inventory_exists_without_hosts(
        self, fake_pkg, monkeypatch, tmp_path
    ):
        """A file someone (or an earlier discovery) left is a choice to respect."""
        placeholder = tmp_path / "remote_clients.toml"
        placeholder.write_text("# no hosts found yet\n")
        monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
        fake_pkg.default_inventory = str(placeholder)
        fake_pkg.inventory_error = fake_pkg.InventoryError("defines no [hosts.*] entries")
        assert qna.discovery_needed() is False

    def test_not_needed_when_the_inventory_has_hosts(self, fake_pkg, monkeypatch):
        monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
        fake_pkg.default_inventory = USER_INVENTORY
        fake_pkg.inventory = [fake_pkg.Target(kind="local", name="local")]
        assert qna.discovery_needed() is False

    async def test_run_writes_the_user_inventory(self, fake_pkg, monkeypatch):
        monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
        fake_pkg.discovered = [fake_pkg.Target(kind="local", name="local")]
        assert await qna.run_discovery() == ["local"]
        assert called(fake_pkg, "run_auto_discovery") == [("run_auto_discovery", USER_INVENTORY)]
        assert qna.inventory_path() == USER_INVENTORY

    async def test_unwritable_location_is_a_value_error(self, fake_pkg, monkeypatch):
        def refuse(path):
            raise fake_pkg.InventoryError(f"cannot write {path}")

        monkeypatch.setattr(qna, "_run_auto_discovery", refuse)
        with pytest.raises(ValueError, match="auto-discovery"):
            await qna.run_discovery()


class TestQnaEvalClientRelevance:
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
            "qna_eval_client_relevance",
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
                "qna_eval_client_relevance",
                {"relevance": "true", "container_images": ["ubuntu; rm -rf /"]},
            )
        assert not [c for c in fake_pkg.calls if c[0] == "evaluate"]

    async def test_timeout_is_clamped(self, gated, fake_pkg):
        fake_pkg.scripted_results = [fake_pkg.FakeResult(host="container:ubuntu:22.04")]
        await call(
            gated,
            "qna_eval_client_relevance",
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
            "qna_eval_client_relevance",
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
            "qna_eval_client_relevance",
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
                "qna_eval_client_relevance",
                {"relevance": "true", "container_images": ["ubuntu:22.04"]},
            )
        audit = [r for r in caplog.records if "BIGFIX QNA" in r.getMessage()]
        assert audit, "every qna run must leave an audit line"

    async def test_qna_list_targets_reports_inventory_and_policy(
        self, gated, fake_pkg, monkeypatch
    ):
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", "/etc/qna-hosts.toml")
        fake_pkg.inventory = [fake_pkg.Target(kind="ssh", name="mac-test")]
        result = await call(gated, "qna_list_targets")
        assert result.data["containers_allowed"] is True
        assert result.data["inventory"] == [
            {"name": "mac-test", "kind": "ssh", "label": "mac-test"}
        ]
        assert result.data["inventory_path"] == "/etc/qna-hosts.toml"
        assert result.data["inventory_source"] == "BIGFIX_QNA_INVENTORY"
        assert result.data["auto_discovery"] is False
        assert result.data["auto_discovery_pending"] is False

    async def test_qna_list_targets_reports_the_default_inventory(
        self, gated, fake_pkg, monkeypatch
    ):
        monkeypatch.delenv("BIGFIX_QNA_INVENTORY", raising=False)
        fake_pkg.default_inventory = USER_INVENTORY
        fake_pkg.inventory = [fake_pkg.Target(kind="local", name="local")]
        result = await call(gated, "qna_list_targets")
        assert result.data["inventory_path"] == USER_INVENTORY
        assert result.data["inventory_source"] == "default search"
        assert result.data["auto_discovery"] is True
        assert result.data["auto_discovery_pending"] is False
        # listing never probes hosts or writes files
        assert not called(fake_pkg, "run_auto_discovery")

    async def test_no_targets_runs_on_every_inventory_host(self, gated, fake_pkg, caplog):
        fake_pkg.default_inventory = USER_INVENTORY
        fake_pkg.inventory = [
            fake_pkg.Target(kind="ssh", name="mac-test"),
            fake_pkg.Target(kind="local", name="local"),
        ]
        fake_pkg.scripted_results = [
            fake_pkg.FakeResult(host="mac-test"),
            fake_pkg.FakeResult(host="local"),
        ]
        with caplog.at_level("INFO"):
            result = await call(gated, "qna_eval_client_relevance", {"relevance": "true"})
        assert result.data["target_count"] == 2
        evaluate = called(fake_pkg, "evaluate")[0]
        assert evaluate[2] == ("mac-test", "local")
        assert not called(fake_pkg, "run_auto_discovery")
        audit = [r.getMessage() for r in caplog.records if "BIGFIX QNA" in r.getMessage()]
        assert any("mac-test" in line and "local" in line for line in audit)

    async def test_no_targets_and_no_inventory_runs_discovery_first(self, gated, fake_pkg, caplog):
        fake_pkg.discovered = [fake_pkg.Target(kind="local", name="local")]
        fake_pkg.scripted_results = [fake_pkg.FakeResult(host="local")]
        with caplog.at_level("INFO"):
            result = await call(gated, "qna_eval_client_relevance", {"relevance": "true"})
        assert called(fake_pkg, "run_auto_discovery") == [("run_auto_discovery", USER_INVENTORY)]
        assert called(fake_pkg, "evaluate")[0][2] == ("local",)
        assert result.data["auto_discovery"] == {"path": USER_INVENTORY, "added": ["local"]}
        audit = [r.getMessage() for r in caplog.records if "BIGFIX QNA" in r.getMessage()]
        assert any("auto-discovery" in line for line in audit)

    async def test_existing_empty_inventory_does_not_discover_by_itself(
        self, gated, fake_pkg, tmp_path
    ):
        placeholder = tmp_path / "remote_clients.toml"
        placeholder.write_text("# no hosts found yet\n")
        fake_pkg.default_inventory = str(placeholder)
        fake_pkg.inventory_error = fake_pkg.InventoryError("defines no [hosts.*] entries")
        with pytest.raises(ToolError, match="qna_discover_targets"):
            await call(gated, "qna_eval_client_relevance", {"relevance": "true"})
        assert not called(fake_pkg, "run_auto_discovery")

    async def test_list_reports_pending_first_run_discovery(self, gated, fake_pkg):
        result = await call(gated, "qna_list_targets")
        assert result.data["inventory_path"] is None
        assert result.data["auto_discovery_pending"] is True
        assert not called(fake_pkg, "run_auto_discovery")

    async def test_discovery_finding_nothing_is_a_clear_error(self, gated, fake_pkg):
        with pytest.raises(ToolError, match="auto-discovery"):
            await call(gated, "qna_eval_client_relevance", {"relevance": "true"})
        assert not called(fake_pkg, "evaluate")

    async def test_explicit_targets_never_run_discovery(self, gated, fake_pkg):
        fake_pkg.scripted_results = [fake_pkg.FakeResult(host="container:ubuntu:22.04")]
        await call(
            gated,
            "qna_eval_client_relevance",
            {"relevance": "true", "container_images": ["ubuntu:22.04"]},
        )
        assert not called(fake_pkg, "run_auto_discovery")

    async def test_discovery_switched_off_is_an_error_not_a_probe(
        self, gated, fake_pkg, monkeypatch
    ):
        monkeypatch.setenv("BIGFIX_QNA_AUTO_DISCOVERY", "0")
        with pytest.raises(ToolError, match="target"):
            await call(gated, "qna_eval_client_relevance", {"relevance": "true"})
        assert not called(fake_pkg, "run_auto_discovery")


class TestQnaDiscoverTargets:
    """The one way to ask for discovery once an inventory file exists."""

    async def test_adds_missing_hosts_to_an_existing_inventory(self, gated, fake_pkg, caplog):
        """Like the CLI's --auto-discovery: add what is missing, evaluate nothing."""
        fake_pkg.default_inventory = USER_INVENTORY
        fake_pkg.inventory = [fake_pkg.Target(kind="ssh", name="mac-test")]
        fake_pkg.discovered = [
            fake_pkg.Target(kind="ssh", name="mac-test"),
            fake_pkg.Target(kind="local", name="local"),
        ]
        with caplog.at_level("INFO"):
            result = await call(gated, "qna_discover_targets")
        assert called(fake_pkg, "run_auto_discovery") == [("run_auto_discovery", USER_INVENTORY)]
        assert result.data["path"] == USER_INVENTORY
        assert result.data["added"] == ["mac-test", "local"]
        assert [row["name"] for row in result.data["inventory"]] == ["mac-test", "local"]
        assert not called(fake_pkg, "evaluate")
        audit = [r.getMessage() for r in caplog.records if "BIGFIX QNA" in r.getMessage()]
        assert any("auto-discovery" in line for line in audit)

    async def test_refused_with_a_pinned_inventory(self, gated, fake_pkg, monkeypatch):
        monkeypatch.setenv("BIGFIX_QNA_INVENTORY", "/etc/qna-hosts.toml")
        with pytest.raises(ToolError, match="BIGFIX_QNA_INVENTORY"):
            await call(gated, "qna_discover_targets")
        assert not called(fake_pkg, "run_auto_discovery")

    @pytest.mark.parametrize(
        "env", [{"BIGFIX_QNA_AUTO_DISCOVERY": "0"}, {"BIGFIX_QNA_INVENTORY": "/etc/q.toml"}]
    )
    async def test_not_registered_when_discovery_is_not_allowed(self, fake_pkg, monkeypatch, env):
        """Registration is the boundary, as for writes."""
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        reloaded = importlib.reload(server)
        try:
            async with Client(reloaded.mcp) as client:
                tools = {tool.name for tool in await client.list_tools()}
            assert "qna_eval_client_relevance" in tools
            assert "qna_discover_targets" not in tools
        finally:
            monkeypatch.undo()  # drops the fake package too
            importlib.reload(server)

    async def test_evaluate_has_no_discovery_parameter(self, gated):
        """Evaluation stays a read; discovering is its own tool call."""
        async with Client(gated.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
        params = tools["qna_eval_client_relevance"].input_schema["properties"]
        assert "auto_discovery" not in params


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

    def test_discovery_calls_match_the_real_signatures(self):
        import inspect

        from bigfix_remote_client_relevance import discovery, inventory_paths

        inspect.signature(discovery.run_auto_discovery).bind(Path("x.toml"))
        inspect.signature(inventory_paths.user_inventory_path).bind()
        assert issubclass(
            importlib.import_module(f"{PKG}.inventory").EmptyInventoryError,
            importlib.import_module(PKG).InventoryError,
        )

    def test_default_search_skips_the_working_directory(self, monkeypatch, tmp_path):
        """An MCP server's cwd is wherever its client launched it."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "remote_clients.toml").write_text("[hosts.cwd]\ntransport = 'local'\n")
        assert qna._find_default_inventory() is None

    def test_default_search_finds_the_user_inventory(self, isolated_home):
        user = isolated_home / ".bigfix" / "remote_clients.toml"
        user.parent.mkdir()
        user.write_text("[hosts.local]\ntransport = 'local'\n")
        assert Path(qna._find_default_inventory()) == user
        assert Path(qna._user_inventory_path()) == user

    def test_default_search_never_sees_the_real_site_config(self, isolated_home):
        """conftest points the all-users config dir into the tmp HOME."""
        import platformdirs

        assert Path(platformdirs.site_config_dir("bigfix")).is_relative_to(isolated_home)

    def test_container_targets_build_with_the_real_target(self):
        targets = qna.resolve_targets(None, ["ubuntu:22.04"])
        assert targets[0].kind == "container"
        assert targets[0].image == "ubuntu:22.04"

    def test_result_payload_has_the_keys_the_server_reads(self, real_pkg):
        # server.py reads error_kind from each shaped result for progress
        assert "error_kind" in real_pkg.ResultPayload.__annotations__

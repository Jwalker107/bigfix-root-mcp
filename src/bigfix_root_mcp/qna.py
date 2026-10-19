"""Local/remote qna evaluation via bigfix_remote_client_relevance.

The package has heavy dependencies (asyncssh, docker), so its import is lazy
and centralized in _qna(): a server that never evaluates client relevance
never pays for it, and tests fake it at the sys.modules seam.

Target policy - the security boundary alongside the registration gate in
server.py: only names from the inventory file plus caller-named container
images. No caller-supplied ssh hosts, users, or become flags; the package's
Target constructor is never exposed to tool parameters. The stubbed
'fastquery' transport is deliberately unexposed.

The inventory is BIGFIX_QNA_INVENTORY when set, else the package CLI's own
default (~/.bigfix/remote_clients.toml, then the all-users config dir). A
run that names no targets covers every inventory host, as the CLI does -
qna only reads. When no inventory file exists at all, the package's non-interactive
auto-discovery fills ~/.bigfix/remote_clients.toml first (the CLI's first
run); once a file exists it runs only through qna_discover_targets. Its interactive
SSH discovery is never used, so the server never prompts.

This module must not import fastmcp (same layering rule as clientquery.py):
it raises ValueError and server.py translates.
"""

import asyncio
import importlib.util
import os
import re
import sys
import tomllib
from pathlib import Path

MAX_QNA_TIMEOUT_SECONDS = 300
MAX_QNA_TARGETS = 20
MAX_PARALLEL = 8
MAX_RAW_OUTPUT = 4000

# docker/podman image references: name[:tag][@digest]. Anything a shell could
# interpret (spaces, semicolons, quotes) fails this and is refused.
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")

_PACKAGE = "bigfix_remote_client_relevance"


def package_available() -> bool:
    """Whether the package can be imported (without importing it)."""
    if _PACKAGE in sys.modules:
        return True
    return importlib.util.find_spec(_PACKAGE) is not None


_OFF = ("0", "false", "no", "off")


def _configured_inventory() -> str:
    """BIGFIX_QNA_INVENTORY as set: a path, an off value, or "" for the default."""
    return os.environ.get("BIGFIX_QNA_INVENTORY", "").strip()


def inventory_disabled() -> bool:
    """BIGFIX_QNA_INVENTORY=0 turns off the inventory (and discovery) entirely."""
    return _configured_inventory().lower() in _OFF


def inventory_source() -> str:
    """Where inventory_path() comes from, for qna_list_targets."""
    if inventory_disabled():
        return "disabled"
    return "BIGFIX_QNA_INVENTORY" if _configured_inventory() else "default search"


def _find_default_inventory() -> str | None:
    """The package CLI's default inventory search, minus the working directory.

    The CLI looks in ./remote_clients.toml first; an MCP server's working
    directory is wherever its client happened to launch it, so a file there
    is not one anyone chose for this server.
    """
    from bigfix_remote_client_relevance import inventory_paths  # noqa: PLC0415 - lazy

    # default_candidates() is cwd first, then ~/.bigfix, then the site dir
    found = inventory_paths.find_inventory_path(inventory_paths.default_candidates()[1:])
    return None if found is None else str(found)


def _user_inventory_path() -> str:
    """~/.bigfix/remote_clients.toml: the one file auto-discovery writes."""
    from bigfix_remote_client_relevance import inventory_paths  # noqa: PLC0415 - lazy

    return str(inventory_paths.user_inventory_path())


def _run_auto_discovery(path: str) -> list[str]:
    """The package's non-interactive discovery; returns the host names added."""
    from bigfix_remote_client_relevance import discovery  # noqa: PLC0415 - lazy

    return discovery.run_auto_discovery(Path(path))


def inventory_path() -> str:
    """The inventory file in use; "" when there is none (or it is disabled)."""
    if inventory_disabled():
        return ""
    return _configured_inventory() or _find_default_inventory() or ""


def discovery_allowed() -> bool:
    """Whether auto-discovery may run at all (by itself or when asked).

    Only with the default inventory: a BIGFIX_QNA_INVENTORY path is the
    admin's file, and discovery only ever writes ~/.bigfix. Set
    BIGFIX_QNA_AUTO_DISCOVERY=0 to never probe or write anything.
    """
    if inventory_disabled() or _configured_inventory():
        return False
    return os.environ.get("BIGFIX_QNA_AUTO_DISCOVERY", "1").strip().lower() not in _OFF


def containers_enabled() -> bool:
    """Whether caller-named container images are allowed as targets.

    Default on: containers are ephemeral and run on the operator's own
    container engine, and without docker/podman a run fails cleanly. Set
    BIGFIX_QNA_CONTAINERS=0 to restrict targets to the inventory file only.
    """
    return os.environ.get("BIGFIX_QNA_CONTAINERS", "1").strip().lower() not in _OFF


def qna_enabled() -> bool:
    """Registration gate: package importable AND at least one target source.

    Read at import, so it never searches for or loads an inventory: unless
    disabled, the inventory counts as a source (it may be found, or
    discovered, later).
    """
    return package_available() and (not inventory_disabled() or containers_enabled())


def _qna():
    """The single import seam for the package (monkeypatched in tests)."""
    import bigfix_remote_client_relevance as pkg  # noqa: PLC0415 - lazy on purpose

    return pkg


def _defines_no_hosts(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return not tomllib.load(handle).get("hosts")
    except (OSError, tomllib.TOMLDecodeError):
        return False


def _load_inventory() -> list:
    path = inventory_path()
    if not path:
        return []
    pkg = _qna()
    try:
        return list(pkg.load_inventory(path))
    except pkg.InventoryError:
        # the package refuses a file with no [hosts.*] (discovery's comment-only
        # placeholder, typically); here that just means no inventory targets
        if _defines_no_hosts(path):
            return []
        raise


def discovery_needed() -> bool:
    """No inventory file exists yet, so an unselected run discovers first.

    Only the missing-file case, as in the package CLI: a file that exists,
    even one with no hosts (an earlier discovery's placeholder, say), was
    left there on purpose, so discovery then waits to be asked for.
    """
    return discovery_allowed() and not inventory_path()


def discovery_refusal() -> str:
    """Why qna_discover_targets is refused here; "" when it is allowed."""
    if inventory_disabled():
        return "auto-discovery is off: BIGFIX_QNA_INVENTORY=0 disables the inventory."
    if _configured_inventory():
        return (
            "auto-discovery only writes ~/.bigfix/remote_clients.toml, and this server "
            "uses the BIGFIX_QNA_INVENTORY file instead; add hosts there."
        )
    if not discovery_allowed():
        return "auto-discovery is off on this server (BIGFIX_QNA_AUTO_DISCOVERY=0)."
    return ""


async def run_discovery() -> list[str]:
    """Run auto-discovery into ~/.bigfix/remote_clients.toml; return hosts added.

    It probes this machine's qna, the container engine's images and
    developer.bigfix.com, which takes a while: off the event loop it goes.
    """
    pkg = _qna()
    try:
        return await asyncio.to_thread(_run_auto_discovery, _user_inventory_path())
    except pkg.InventoryError as exc:
        raise ValueError(f"qna auto-discovery failed: {exc}") from exc


def list_inventory_targets() -> list[dict]:
    """Name/kind/label rows for the inventory in use; [] when there is none."""
    return [
        {"name": target.name, "kind": target.kind, "label": target.label}
        for target in _load_inventory()
    ]


def resolve_targets(inventory_names: list[str] | None, container_images: list[str] | None) -> list:
    """Validate tool input and build the Target list.

    Inventory targets are returned exactly as the inventory configures them
    (user/become/verify stay its own); container targets are built from the
    image name alone. Selecting nothing selects every inventory host.
    Everything else is refused with a ValueError.
    """
    targets: list = []

    if not inventory_names and not container_images:
        # nothing selected: every inventory host, as the package CLI does
        targets = _load_inventory()
        if not targets:
            hint = " or run qna_discover_targets," if discovery_allowed() else ""
            raise ValueError(
                "no targets: pass inventory_targets and/or container_images,"
                f"{hint} or add hosts to the inventory (see qna_list_targets)."
            )
        if len(targets) > MAX_QNA_TARGETS:
            raise ValueError(
                f"the inventory has {len(targets)} hosts, over the {MAX_QNA_TARGETS} "
                "maximum per run; choose some with inventory_targets."
            )
        return targets

    if inventory_names:
        path = inventory_path()
        if not path:
            raise ValueError(
                "inventory_targets were requested but no inventory was found - "
                "create ~/.bigfix/remote_clients.toml or set BIGFIX_QNA_INVENTORY "
                "to a remote_clients.toml path on the server."
            )
        by_name = {target.name: target for target in _load_inventory()}
        for name in inventory_names:
            target = by_name.get(name)
            if target is None:
                known = ", ".join(sorted(by_name)) or "(none)"
                raise ValueError(f"unknown inventory target {name!r}; inventory has: {known}")
            targets.append(target)

    if container_images:
        if not containers_enabled():
            raise ValueError(
                "container targets are disabled on this server "
                "(BIGFIX_QNA_CONTAINERS is off); use inventory_targets instead."
            )
        pkg = _qna()
        for image in container_images:
            if not _IMAGE_RE.match(image):
                raise ValueError(f"invalid container image name: {image!r}")
            targets.append(pkg.Target(kind="container", name=image, image=image))

    if not targets:
        raise ValueError("no targets: pass inventory_targets and/or container_images.")
    if len(targets) > MAX_QNA_TARGETS:
        raise ValueError(f"too many targets: {len(targets)} > {MAX_QNA_TARGETS} maximum.")
    return targets


def shape_result(result) -> dict:
    """One evaluation result as a wire-shaped dict, raw qna output capped."""
    return _qna().result_to_dict(result, max_raw_output=MAX_RAW_OUTPUT)

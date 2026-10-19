"""Local/remote qna evaluation via bigfix_remote_client_relevance.

The package has heavy dependencies (asyncssh, docker), so its import is lazy
and centralized in _qna(): a server that never evaluates client relevance
never pays for it, and tests fake it at the sys.modules seam.

Target policy - the security boundary alongside the registration gate in
server.py: only names from the admin-configured inventory file plus
caller-named container images. No caller-supplied ssh hosts, users, or
become flags; the package's Target constructor is never exposed to tool
parameters. The stubbed 'fastquery' transport is deliberately unexposed.

This module must not import fastmcp (same layering rule as clientquery.py):
it raises ValueError and server.py translates.
"""

import importlib.util
import os
import re
import sys

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


def inventory_path() -> str:
    """The admin-configured hosts.toml path; "" when not configured."""
    return os.environ.get("BIGFIX_QNA_INVENTORY", "").strip()


def containers_enabled() -> bool:
    """Whether caller-named container images are allowed as targets.

    Default on: containers are ephemeral and run on the operator's own
    container engine, and without docker/podman a run fails cleanly. Set
    BIGFIX_QNA_CONTAINERS=0 to restrict targets to the inventory file only.
    """
    return os.environ.get("BIGFIX_QNA_CONTAINERS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def qna_enabled() -> bool:
    """Registration gate: package importable AND at least one target source."""
    return package_available() and (bool(inventory_path()) or containers_enabled())


def _qna():
    """The single import seam for the package (monkeypatched in tests)."""
    import bigfix_remote_client_relevance as pkg  # noqa: PLC0415 - lazy on purpose

    return pkg


def _load_inventory() -> list:
    path = inventory_path()
    if not path:
        return []
    return list(_qna().load_inventory(path))


def list_inventory_targets() -> list[dict]:
    """Name/kind/label rows for the admin-configured inventory; [] when unset."""
    return [
        {"name": target.name, "kind": target.kind, "label": target.label}
        for target in _load_inventory()
    ]


def resolve_targets(inventory_names: list[str] | None, container_images: list[str] | None) -> list:
    """Validate tool input and build the Target list.

    Inventory targets are returned exactly as the admin configured them
    (user/become/verify stay theirs); container targets are built from the
    image name alone. Everything else is refused with a ValueError.
    """
    targets: list = []

    if inventory_names:
        path = inventory_path()
        if not path:
            raise ValueError(
                "inventory_targets were requested but no inventory is configured - "
                "set BIGFIX_QNA_INVENTORY to a hosts.toml path on the server."
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

"""Instance snapshots; ownership metadata travels with them."""

import re
from datetime import datetime
from pathlib import Path

from nixant.errors import NixantError, UsageError
from nixant.models import MachineState, Snapshot
from nixant.ownership import PREFIX
from nixant.project import project_id
from nixant.providers.base import Provider


def default_name(now: datetime | None = None) -> str:
    return "nixant-" + (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def validate_name(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", name) is None:
        raise UsageError(
            f"invalid snapshot name {name!r}: use letters, digits, dots, "
            "underscores and dashes, starting with a letter or digit"
        )
    return name


def find(provider: Provider, state: MachineState, name: str) -> Snapshot:
    snapshots = provider.snapshot_list(state.name)
    for snapshot in snapshots:
        if snapshot.name == name:
            return snapshot
    available = ", ".join(s.name for s in snapshots) or "(none)"
    raise NixantError(f"{state.name} has no snapshot {name!r}; available: {available}")


def create(provider: Provider, state: MachineState, name: str) -> None:
    if any(s.name == name for s in provider.snapshot_list(state.name)):
        raise NixantError(f"{state.name} already has a snapshot named {name!r}")
    provider.snapshot_create(state.name, name)


def restore(provider: Provider, state: MachineState, name: str, root: Path) -> None:
    """Roll back, then re-assert this checkout's ownership of the instance.

    The snapshot carries the metadata of its time (system, activation, root);
    a checkout that was adopted since must not lose the instance to the old root.
    """
    find(provider, state, name)
    provider.snapshot_restore(state.name, name)
    provider.set_metadata(
        state.name,
        {PREFIX + "project": project_id(root), PREFIX + "root": str(root)},
    )

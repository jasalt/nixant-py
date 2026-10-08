"""Instance snapshots; ownership metadata travels with them."""

import re
from datetime import datetime
from pathlib import Path

from nixant.adopt import mount_moves, rebase_mounts
from nixant.errors import CommandError, NixantError, UsageError
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

    The snapshot carries the metadata and mount sources of its time; a
    checkout that was adopted since must not lose the instance to the old
    root. Incus restarts a running instance as part of the restore, and that
    start fails on a mount source that no longer exists, so such an instance
    is stopped first and started again once its mounts point here.
    """
    snapshot = find(provider, state, name)
    old_root = snapshot.config.get(PREFIX + "root", "")
    moves = mount_moves(snapshot.devices, old_root, root) if old_root else {}
    restart = bool(moves) and state.status != "Stopped"
    if restart:
        if state.ephemeral:
            raise NixantError(
                f"snapshot {name} mounts the old checkout {old_root}, and stopping "
                f"ephemeral {state.name} to repair that would delete it"
            )
        if state.status == "Frozen":
            provider.start(state.name)
        try:
            provider.stop(state.name)
        except CommandError as exc:
            raise NixantError(
                f"could not stop {state.name} before restoring; "
                f"run nixant down --force first\n{exc}"
            ) from exc
    provider.snapshot_restore(state.name, name)
    rebase_mounts(provider, state.name, snapshot.devices, old_root, root)
    provider.set_metadata(
        state.name,
        {PREFIX + "project": project_id(root), PREFIX + "root": str(root)},
    )
    if restart:
        provider.start(state.name)

"""Re-home an instance whose checkout moved, without evaluating anything."""

import os
from collections.abc import Mapping
from pathlib import Path

from nixant.errors import NixantError
from nixant.models import MachineState, MountSpec
from nixant.nix.build import gcroot_path
from nixant.ownership import PREFIX, check_schema
from nixant.planner import MOUNT_PREFIX, Change, Effect, SetDevice
from nixant.project import project_id, state_directory
from nixant.providers.base import Provider
from nixant.run import Runner


def candidates(provider: Provider, target: str) -> list[MachineState]:
    """Managed instances of this target whose recorded checkout is gone."""
    found = provider.find({PREFIX + "managed": "true", PREFIX + "target": target})
    return [s for s in found if not Path(s.config.get(PREFIX + "root", "")).is_dir()]


def choose(
    provider: Provider, root: Path, target: str, instance: str | None
) -> MachineState:
    if instance is not None:
        state = provider.inspect(instance)
        if state is None:
            raise NixantError(f"instance {instance} does not exist")
        if state.config.get(PREFIX + "managed") != "true":
            raise NixantError(f"instance {instance} is not managed by nixant")
        recorded = state.config.get(PREFIX + "target")
        if recorded != target:
            raise NixantError(
                f"instance {instance} belongs to target {recorded}, not {target}; "
                "adopt never changes the target"
            )
        old_root = state.config.get(PREFIX + "root", "")
        if old_root and Path(old_root).is_dir() and Path(old_root).resolve() != root:
            raise NixantError(
                f"the checkout of {instance} still exists at {old_root}; "
                "nothing to adopt"
            )
        return state
    found = candidates(provider, target)
    if not found:
        raise NixantError(
            f"no instance of target {target} has a missing checkout; nothing to adopt"
        )
    if len(found) > 1:
        listing = "\n".join(
            f"  {s.name} (was {s.config.get(PREFIX + 'root', 'unknown')})"
            for s in found
        )
        raise NixantError(
            f"several instances could be adopted for target {target}; "
            f"choose one with --instance:\n{listing}"
        )
    return found[0]


def remap_source(source: str, old_root: str, new_root: Path) -> str:
    """Move a mount source from the old checkout to the new one; keep others."""
    old = old_root.rstrip("/")
    if source == old:
        return str(new_root)
    if old and source.startswith(old + "/"):
        return str(new_root / source[len(old) + 1 :])
    return source


def mount_moves(
    devices: Mapping[str, Mapping[str, str]], old_root: str, new_root: Path
) -> dict[str, str]:
    """Managed mount devices whose source lies in the old checkout, rebased."""
    moves = {}
    for device, settings in sorted(devices.items()):
        if not device.startswith(MOUNT_PREFIX):
            continue
        new_source = remap_source(settings.get("source", ""), old_root, new_root)
        if new_source != settings.get("source"):
            moves[device] = new_source
    return moves


def rebase_mounts(
    provider: Provider,
    name: str,
    devices: Mapping[str, Mapping[str, str]],
    old_root: str,
    new_root: Path,
) -> None:
    for device, new_source in mount_moves(devices, old_root, new_root).items():
        provider.apply(
            name,
            Change(
                "mounts",
                Effect.LIVE,
                f"point {device} at {new_source}",
                SetDevice(device, {"source": new_source}),
            ),
        )


def adopt(
    provider: Provider, runner: Runner, root: Path, target: str, state: MachineState
) -> None:
    check_schema(state)
    old_root = state.config.get(PREFIX + "root", "")
    old_id = state.config.get(PREFIX + "project", "")
    # Protect the closure under the new name before changing anything else.
    retired = _register_gcroot(old_id, root, target, runner)
    rebase_mounts(provider, state.name, state.devices, old_root, root)
    if state.status == "Running":
        # A running VM drops a retargeted mount until virtiofs is re-plugged.
        moved = [
            MountSpec(
                device.removeprefix(MOUNT_PREFIX),
                source,
                state.devices[device].get("path", ""),
                state.devices[device].get("readonly") == "true",
            )
            for device, source in mount_moves(state.devices, old_root, root).items()
        ]
        provider.verify_mounts(state.name, moved)
    provider.set_metadata(
        state.name,
        {PREFIX + "project": project_id(root), PREFIX + "root": str(root)},
    )
    if retired is not None:
        try:
            retired.unlink(missing_ok=True)
        except OSError as exc:
            raise NixantError(f"cannot remove old GC root {retired}: {exc}") from exc


def _register_gcroot(
    old_id: str, root: Path, target: str, runner: Runner
) -> Path | None:
    """Register the old link's system under the new link; return the old link.

    Nix records an out-link indirectly, by the link's own pathname, so renaming
    the link would leave that record dangling and the closure unprotected.
    """
    old = state_directory() / "gcroots" / f"{old_id}-{target}"
    new = gcroot_path(root, target)
    if old == new or not old.is_symlink():
        return None
    if not old.exists():
        return old  # already collected; nothing left to protect
    system = os.readlink(old)
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        runner.run(["nix", "build", system, "--out-link", str(new)], capture=True)
    except (OSError, NixantError) as exc:
        raise NixantError(
            f"cannot register GC root {new} for {system}; nothing was changed: {exc}"
        ) from exc
    return old

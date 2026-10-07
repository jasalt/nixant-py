"""Re-home an instance whose checkout moved, without evaluating anything."""

import os
from pathlib import Path

from nixant.errors import NixantError
from nixant.models import MachineState
from nixant.nix.build import gcroot_path
from nixant.ownership import PREFIX, check_schema
from nixant.planner import MOUNT_PREFIX, Change, Effect
from nixant.project import project_id, state_directory
from nixant.providers.base import Provider


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


def adopt(provider: Provider, root: Path, target: str, state: MachineState) -> None:
    check_schema(state)
    old_root = state.config.get(PREFIX + "root", "")
    old_id = state.config.get(PREFIX + "project", "")
    for device, settings in sorted(state.devices.items()):
        if not device.startswith(MOUNT_PREFIX):
            continue
        new_source = remap_source(settings.get("source", ""), old_root, root)
        if new_source != settings.get("source"):
            provider.apply(
                state.name,
                Change(
                    "mounts",
                    Effect.LIVE,
                    f"point {device} at {new_source}",
                    "device-set",
                    device,
                    {"source": new_source},
                ),
            )
    provider.set_metadata(
        state.name,
        {PREFIX + "project": project_id(root), PREFIX + "root": str(root)},
    )
    _move_gcroot(old_id, root, target)


def _move_gcroot(old_id: str, root: Path, target: str) -> None:
    old = state_directory() / "gcroots" / f"{old_id}-{target}"
    new = gcroot_path(root, target)
    try:
        if old != new and old.is_symlink():
            new.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old, new)
    except OSError as exc:
        raise NixantError(f"cannot move GC root {old}: {exc}") from exc

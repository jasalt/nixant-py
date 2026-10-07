"""Desired vs actual machine settings, classified by how they can be applied.

Only nixant-prefixed devices, the limits keys, and the ``size`` key of the
instance-local root device are ever considered; everything else on the
instance (and every profile) is left alone.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from nixant.models import MachineSpec, MachineState

MOUNT_PREFIX = "nixant-mount-"
PORT_PREFIX = "nixant-port-"

_UNITS = {
    "": 1,
    "B": 1,
    "kB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "PB": 10**15,
    "KiB": 2**10,
    "MiB": 2**20,
    "GiB": 2**30,
    "TiB": 2**40,
    "PiB": 2**50,
}


class Effect(Enum):
    LIVE = "live"
    RESTART = "restart"
    RECREATE = "recreate"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Change:
    """One reconciliation step; ``op`` tells the provider how to apply it."""

    setting: str
    effect: Effect
    summary: str
    op: str = ""
    key: str = ""
    values: Mapping[str, str] = field(default_factory=dict)


def parse_size(value: str) -> int | None:
    """Bytes for an Incus size string, or None when it is not a plain size."""
    match = re.fullmatch(r"(\d+)\s*([A-Za-z]*)", value.strip())
    if match is None or match[2] not in _UNITS:
        return None
    return int(match[1]) * _UNITS[match[2]]


def mount_device(mount_source: str, target: str, read_only: bool) -> dict[str, str]:
    return {
        "source": mount_source,
        "path": target,
        "shift": "true",
        "readonly": "true" if read_only else "false",
    }


def _device_changes(
    prefix: str,
    kind: str,
    desired: Mapping[str, Mapping[str, str]],
    actual: Mapping[str, Mapping[str, str]],
    effect: Effect,
    label: str,
) -> list[Change]:
    changes: list[Change] = []
    for name, wanted in sorted(desired.items()):
        current = actual.get(name)
        if current is None:
            changes.append(
                Change(label, effect, f"add {name}", "device-add", name, wanted)
            )
        elif current.get("type", kind) != kind:
            changes.append(
                Change(
                    label,
                    Effect.UNSUPPORTED,
                    f"{name} exists but is not a {kind} device",
                )
            )
        else:
            drift = {
                key: value
                for key, value in wanted.items()
                if current.get(key, "false" if key == "readonly" else "") != value
            }
            if drift:
                changes.append(
                    Change(label, effect, f"update {name}", "device-set", name, drift)
                )
    for name in sorted(actual):
        if name.startswith(prefix) and name not in desired:
            changes.append(
                Change(label, effect, f"remove {name}", "device-remove", name)
            )
    return changes


def plan(spec: MachineSpec, state: MachineState) -> list[Change]:
    changes: list[Change] = []
    if state.kind != ("virtual-machine" if spec.kind == "vm" else spec.kind):
        changes.append(
            Change(
                "kind",
                Effect.RECREATE,
                f"instance is a {state.kind}, configuration wants a {spec.kind}; "
                "destroy and recreate it",
            )
        )
        return changes
    # Config and root-disk limits apply live to containers and VMs alike.
    if spec.cpus is not None and state.config.get("limits.cpu") != str(spec.cpus):
        changes.append(
            Change(
                "cpus",
                Effect.LIVE,
                f"set limits.cpu={spec.cpus}",
                "config",
                "limits.cpu",
                {"value": str(spec.cpus)},
            )
        )
    if spec.memory_bytes is not None:
        current = state.config.get("limits.memory")
        if current is None or parse_size(current) != spec.memory_bytes:
            changes.append(
                Change(
                    "memory",
                    Effect.LIVE,
                    f"set limits.memory={spec.memory_bytes}B",
                    "config",
                    "limits.memory",
                    {"value": str(spec.memory_bytes)},
                )
            )
    if spec.disk_bytes is not None:
        changes.extend(_disk(spec.disk_bytes, state))
    mount_effect = Effect.RESTART if spec.kind == "vm" else Effect.LIVE
    changes.extend(
        _device_changes(
            MOUNT_PREFIX,
            "disk",
            {
                f"{MOUNT_PREFIX}{mount.name}": mount_device(
                    mount.source, mount.target, mount.read_only
                )
                for mount in spec.mounts
            },
            state.devices,
            mount_effect,
            "mounts",
        )
    )
    changes.extend(
        _device_changes(
            PORT_PREFIX,
            "proxy",
            {
                f"{PORT_PREFIX}{port.host}": {
                    "listen": f"tcp:{port.address}:{port.host}",
                    "connect": f"tcp:127.0.0.1:{port.guest}",
                }
                for port in spec.ports
            },
            state.devices,
            Effect.LIVE,
            "ports",
        )
    )
    return changes


def _disk(wanted: int, state: MachineState) -> list[Change]:
    root = state.expanded_devices.get("root", {})
    current_text = root.get("size")
    current = parse_size(current_text) if current_text else None
    if current is not None and wanted < current:
        return [
            Change(
                "disk",
                Effect.UNSUPPORTED,
                f"cannot shrink the root disk from {current_text} to {wanted}B",
            )
        ]
    if current == wanted:
        return []
    return [
        Change(
            "disk",
            Effect.LIVE,
            f"set root size={wanted}B",
            "root-size",
            "root",
            {"size": str(wanted), "local": "true" if "root" in state.devices else ""},
        )
    ]

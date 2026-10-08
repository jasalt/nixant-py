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
    unique: str,
) -> list[Change]:
    """Removals first, then in-place updates, then additions.

    Incus rejects two devices sharing a ``unique`` value (a disk path, a proxy
    listen address) at every step, so a value must be released before another
    device takes it. An update that takes a value still held by a different
    managed device is turned into remove-then-add, which also breaks swaps.
    """
    removals: list[Change] = []
    updates: list[Change] = []
    additions: list[Change] = []
    obsolete = {
        name for name in actual if name.startswith(prefix) and name not in desired
    }
    held = {
        device.get(unique): name
        for name, device in actual.items()
        if name.startswith(prefix) and name not in obsolete
    }
    for name, wanted in sorted(desired.items()):
        current = actual.get(name)
        if current is None:
            additions.append(
                Change(label, effect, f"add {name}", "device-add", name, wanted)
            )
        elif current.get("type", kind) != kind:
            updates.append(
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
            if not drift:
                continue
            if unique in drift and held.get(drift[unique], name) != name:
                removals.append(
                    Change(label, effect, f"remove {name}", "device-remove", name)
                )
                additions.append(
                    Change(label, effect, f"re-add {name}", "device-add", name, wanted)
                )
            else:
                updates.append(
                    Change(label, effect, f"update {name}", "device-set", name, drift)
                )
    for name in sorted(obsolete):
        removals.append(Change(label, effect, f"remove {name}", "device-remove", name))
    return removals + updates + additions


def plan(spec: MachineSpec, state: MachineState) -> list[Change]:
    changes: list[Change] = []
    if state.kind != spec.kind:
        changes.append(
            Change(
                "kind",
                Effect.RECREATE,
                f"instance is a {state.kind}, configuration wants a {spec.kind}; "
                "destroy and recreate it",
            )
        )
        return changes
    if state.ephemeral != spec.ephemeral:
        # Incus fixes this at creation; changing it means a new instance.
        changes.append(
            Change(
                "ephemeral",
                Effect.RECREATE,
                f"instance is {'ephemeral' if state.ephemeral else 'persistent'}, "
                f"configuration wants {'ephemeral' if spec.ephemeral else 'persistent'}"
                "; destroy and recreate it",
            )
        )
        return changes
    # CPU and memory limits apply live to containers and VMs alike.
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
        changes.extend(_disk(spec.disk_bytes, state, spec.kind == "vm"))
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
            Effect.LIVE,  # virtiofs hot-plug, retarget and removal work on running VMs
            "mounts",
            "path",
        )
    )
    if spec.kind == "vm" and spec.ports:
        changes.append(
            Change(
                "ports",
                Effect.UNSUPPORTED,
                "ports are not supported on VMs: Incus only allows NAT-mode proxies "
                "there, which need a static IPv4 address on the instance NIC",
            )
        )
    else:
        changes.extend(_ports(spec, state))
    return changes


def _ports(spec: MachineSpec, state: MachineState) -> list[Change]:
    return list(
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
            "listen",
        )
    )


def _disk(wanted: int, state: MachineState, vm: bool) -> list[Change]:
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
    # A running VM only sees the larger disk after its next boot.
    return [
        Change(
            "disk",
            Effect.RESTART if vm else Effect.LIVE,
            f"set root size={wanted}B",
            "root-size",
            "root",
            {"size": str(wanted), "local": "true" if "root" in state.devices else ""},
        )
    ]

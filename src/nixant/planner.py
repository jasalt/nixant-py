"""Desired vs actual machine settings, classified by how they can be applied.

Only nixant-prefixed devices, the limits keys, and the ``size`` key of the
instance-local root device are ever considered; everything else on the
instance (and every profile) is left alone.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from nixant.errors import NixantError
from nixant.models import MachineSpec, MachineState, MountSpec

MOUNT_PREFIX = "nixant-mount-"
PORT_PREFIX = "nixant-port-"
# Temporary `nixant forward` devices; reconciliation never touches them.
FORWARD_PREFIX = "nixant-forward-"
WAYLAND_DEVICE = "nixant-wayland"
# /dev is the one guest directory that exists before the proxy starts.
WAYLAND_LISTEN = "/dev/nixant-wayland-0"
GPU_DEVICE = "nixant-gpu"

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


DeviceKind = Literal["disk", "proxy", "unix-char"]


@dataclass(frozen=True)
class Host:
    """What the calling host session offers the guest's desktop settings."""

    wayland_socket: str | None = None
    render_node: str | None = None


NO_HOST = Host()


@dataclass(frozen=True)
class SetConfig:
    key: str
    value: str


@dataclass(frozen=True)
class AddDevice:
    name: str
    kind: DeviceKind
    values: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class SetDevice:
    name: str
    values: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class RemoveDevice:
    name: str


@dataclass(frozen=True)
class SetRootSize:
    """Only the size key of the root device; an inherited one is overridden."""

    size: int
    inherited: bool


Action = SetConfig | AddDevice | SetDevice | RemoveDevice | SetRootSize


@dataclass(frozen=True)
class Change:
    """One reconciliation step; ``action`` is what the provider applies."""

    setting: str
    effect: Effect
    summary: str
    action: Action | None = None

    def __post_init__(self) -> None:
        applicable = self.effect in (Effect.LIVE, Effect.RESTART)
        if applicable != (self.action is not None):
            raise ValueError(
                f"{self.effect.value} change {self.summary!r} must "
                + ("carry an action" if applicable else "not carry an action")
            )


def parse_size(value: str) -> int | None:
    """Bytes for an Incus size string, or None when it is not a plain size."""
    match = re.fullmatch(r"(\d+)\s*([A-Za-z]*)", value.strip())
    if match is None or match[2] not in _UNITS:
        return None
    return int(match[1]) * _UNITS[match[2]]


def check_mount(mount: MountSpec) -> None:
    """Reject a mount Incus cannot attach, before anything is built or created."""
    if not mount.name or "/" in mount.name or "\0" in mount.name:
        raise NixantError(f"invalid mount name {mount.name!r}")
    source = Path(mount.source)
    if not source.is_absolute() or not source.exists():
        raise NixantError(f"mount {mount.name} requires an existing absolute source")


def mount_device(mount: MountSpec) -> dict[str, str]:
    """The one definition of a mount's disk device; every change is planned here."""
    return {
        "source": mount.source,
        "path": mount.target,
        "shift": "true",
        "readonly": "true" if mount.read_only else "false",
    }


def remount(mount: MountSpec) -> list[Change]:
    """Detach and attach a mount again, for one that never showed up in the guest."""
    name = f"{MOUNT_PREFIX}{mount.name}"
    return [
        Change("mounts", Effect.LIVE, f"remove {name}", RemoveDevice(name)),
        Change(
            "mounts",
            Effect.LIVE,
            f"re-add {name}",
            AddDevice(name, "disk", mount_device(mount)),
        ),
    ]


def _device_changes(
    prefix: str,
    kind: DeviceKind,
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
                Change(label, effect, f"add {name}", AddDevice(name, kind, wanted))
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
                    Change(label, effect, f"remove {name}", RemoveDevice(name))
                )
                additions.append(
                    Change(
                        label, effect, f"re-add {name}", AddDevice(name, kind, wanted)
                    )
                )
            else:
                updates.append(
                    Change(label, effect, f"update {name}", SetDevice(name, drift))
                )
    for name in sorted(obsolete):
        removals.append(Change(label, effect, f"remove {name}", RemoveDevice(name)))
    return removals + updates + additions


def plan(spec: MachineSpec, state: MachineState, host: Host = NO_HOST) -> list[Change]:
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
                SetConfig("limits.cpu", str(spec.cpus)),
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
                    SetConfig("limits.memory", str(spec.memory_bytes)),
                )
            )
    if spec.disk_bytes is not None:
        changes.extend(_disk(spec.disk_bytes, state, spec.kind == "vm"))
    changes.extend(
        _device_changes(
            MOUNT_PREFIX,
            "disk",
            {
                f"{MOUNT_PREFIX}{mount.name}": mount_device(mount)
                for mount in spec.mounts
            },
            state.devices,
            Effect.LIVE,  # virtiofs hot-plug, retarget and removal work on running VMs
            "mounts",
            "path",
        )
    )
    unsupported = validate(spec, host)
    changes.extend(unsupported)
    if not any(change.setting == "ports" for change in unsupported):
        changes.extend(_ports(spec, state))
    if not any(change.setting == "wayland" for change in unsupported):
        changes.extend(_wayland(spec, state, host.wayland_socket))
    if not any(change.setting == "gpu" for change in unsupported):
        changes.extend(_gpu(spec, state, host.render_node))
    return changes


def validate(spec: MachineSpec, host: Host = NO_HOST) -> list[Change]:
    """Settings no instance can take, known before one is inspected or created."""
    changes = []
    if spec.kind == "vm" and spec.ports:
        changes.append(
            Change(
                "ports",
                Effect.UNSUPPORTED,
                "ports are not supported on VMs: Incus only allows NAT-mode proxies "
                "there, which need a static IPv4 address on the instance NIC",
            )
        )
    if spec.kind == "vm" and spec.wayland:
        changes.append(
            Change(
                "wayland",
                Effect.UNSUPPORTED,
                "wayland is not supported on VMs: Incus cannot proxy a unix "
                "socket into a VM",
            )
        )
    if spec.kind == "vm" and spec.gpu_gid is not None:
        changes.append(
            Change(
                "gpu",
                Effect.UNSUPPORTED,
                "gpu is not supported on VMs: a render node can only be shared "
                "with a container",
            )
        )
    elif spec.gpu_gid is not None and host.render_node is None:
        changes.append(
            Change(
                "gpu",
                Effect.UNSUPPORTED,
                "gpu: the host has no render node (/dev/dri/renderD*)",
            )
        )
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


def _wayland(
    spec: MachineSpec, state: MachineState, socket: str | None
) -> list[Change]:
    """The proxy re-resolves ``connect`` per connection, so it outlives the
    host session; without a session to read it from, the recorded one stays."""
    desired = {}
    if spec.wayland:
        recorded = state.devices.get(WAYLAND_DEVICE, {}).get("connect")
        default = f"unix:/run/user/{spec.user.uid}/wayland-0"
        uid, gid = str(spec.user.uid), str(spec.user.gid)
        desired[WAYLAND_DEVICE] = {
            "bind": "container",
            "connect": f"unix:{socket}" if socket else recorded or default,
            "listen": f"unix:{WAYLAND_LISTEN}",
            "uid": uid,
            "gid": gid,
            "mode": "0600",
            # Connect to the compositor as the host user, not as root.
            "security.uid": uid,
            "security.gid": gid,
        }
    return _device_changes(
        WAYLAND_DEVICE,
        "proxy",
        desired,
        state.devices,
        Effect.LIVE,
        "wayland",
        "listen",
    )


def _gpu(spec: MachineSpec, state: MachineState, node: str | None) -> list[Change]:
    """Only the render node: no display (card) node, so no modesetting."""
    desired = {}
    if spec.gpu_gid is not None and node is not None:  # validate() refused None
        desired[GPU_DEVICE] = {
            "source": node,
            "path": node,
            "gid": str(spec.gpu_gid),
            "mode": "0660",
        }
    return _device_changes(
        GPU_DEVICE, "unix-char", desired, state.devices, Effect.LIVE, "gpu", "path"
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
            SetRootSize(wanted, inherited="root" not in state.devices),
        )
    ]

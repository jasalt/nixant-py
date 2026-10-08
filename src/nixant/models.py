"""Immutable runtime values shared by evaluation, planning, and providers."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from nixant.errors import NixantError

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class MachineState:
    name: str
    status: str
    kind: str
    config: Mapping[str, str]
    devices: Mapping[str, Mapping[str, str]]
    ipv4: tuple[str, ...] = ()
    created_at: str = ""
    ephemeral: bool = False
    expanded_devices: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))
        object.__setattr__(
            self,
            "devices",
            MappingProxyType(
                {
                    name: MappingProxyType(dict(device))
                    for name, device in self.devices.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "expanded_devices",
            MappingProxyType(
                {
                    name: MappingProxyType(dict(device))
                    for name, device in self.expanded_devices.items()
                }
            ),
        )


@dataclass(frozen=True)
class Snapshot:
    name: str
    created_at: str = ""
    stateful: bool = False
    # The instance config and local devices that a restore brings back.
    config: Mapping[str, str] = field(default_factory=dict, compare=False)
    devices: Mapping[str, Mapping[str, str]] = field(
        default_factory=dict, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))
        object.__setattr__(
            self,
            "devices",
            MappingProxyType(
                {
                    name: MappingProxyType(dict(device))
                    for name, device in self.devices.items()
                }
            ),
        )


@dataclass(frozen=True)
class UserSpec:
    name: str
    uid: int
    gid: int
    home: str
    shell: str


@dataclass(frozen=True)
class MountSpec:
    name: str
    source: str
    target: str
    read_only: bool = False


@dataclass(frozen=True)
class PortSpec:
    host: int
    guest: int
    address: str = "127.0.0.1"


@dataclass(frozen=True)
class MachineSpec:
    instance_name: str
    kind: str
    user: UserSpec
    mounts: tuple[MountSpec, ...]
    workdir: str
    cpus: int | None = None
    memory_bytes: int | None = None
    disk_bytes: int | None = None
    ports: tuple[PortSpec, ...] = ()
    ephemeral: bool = False

    def to_runtime(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "kind": self.kind,
            "instanceName": self.instance_name,
            "user": {
                "name": self.user.name,
                "uid": self.user.uid,
                "gid": self.user.gid,
                "home": self.user.home,
                "shell": self.user.shell,
            },
            "cpus": self.cpus,
            "memoryBytes": self.memory_bytes,
            "diskBytes": self.disk_bytes,
            "mounts": {
                mount.name: {
                    "source": mount.source,
                    "target": mount.target,
                    "readOnly": mount.read_only,
                }
                for mount in self.mounts
            },
            "ports": [
                {"host": port.host, "guest": port.guest, "address": port.address}
                for port in self.ports
            ],
            "workdir": self.workdir,
            "ephemeral": self.ephemeral,
        }

    @classmethod
    def from_runtime(cls, data: Any) -> "MachineSpec":
        """Reject schema drift and malformed JSON at the evaluation boundary."""
        if not isinstance(data, dict):
            raise NixantError("invalid nixant runtime: expected an object")
        if (
            type(data.get("schemaVersion")) is not int
            or data["schemaVersion"] != SCHEMA_VERSION
        ):
            raise NixantError(
                f"nixant runtime schema {data.get('schemaVersion')!r} is unsupported; "
                "run nix flake update nixant or use a matching nixant version"
            )
        try:
            if not isinstance(data["ports"], list):
                raise ValueError("ports must be an array")
            user = data["user"]
            spec = cls(
                instance_name=_string(data["instanceName"]),
                kind=_string(data["kind"]),
                user=UserSpec(
                    name=_string(user["name"]),
                    uid=_integer(user["uid"]),
                    gid=_integer(user["gid"]),
                    home=_absolute(user["home"]),
                    shell=_absolute(user["shell"]),
                ),
                mounts=tuple(
                    MountSpec(
                        name=_string(name),
                        source=_string(mount["source"]),
                        target=_absolute(mount["target"]),
                        read_only=_boolean(mount["readOnly"]),
                    )
                    for name, mount in data["mounts"].items()
                ),
                workdir=_absolute(data["workdir"]),
                ephemeral=_boolean(data.get("ephemeral", False)),
                cpus=_optional_integer(data["cpus"]),
                memory_bytes=_optional_integer(data["memoryBytes"]),
                disk_bytes=_optional_integer(data["diskBytes"]),
                ports=tuple(
                    PortSpec(
                        host=_port(port["host"]),
                        guest=_port(port["guest"]),
                        address=_string(port["address"]),
                    )
                    for port in data["ports"]
                ),
            )
            if (
                re.fullmatch(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", spec.instance_name)
                is None
            ):
                raise ValueError("invalid Incus instance name")
            if spec.kind not in ("container", "vm"):
                raise ValueError("kind must be container or vm")
            if spec.user.name == "root":
                raise ValueError("guest user must not be root")
            if len({m.target for m in spec.mounts}) != len(spec.mounts):
                raise ValueError("mount targets must be unique")
            if len({p.host for p in spec.ports}) != len(spec.ports):
                raise ValueError("host ports must be unique")
            return spec
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise NixantError(f"invalid nixant runtime: {exc}") from exc


def _string(value: Any) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("expected a nonempty string without NUL")
    return value


def _absolute(value: Any) -> str:
    result = _string(value)
    if not result.startswith("/"):
        raise ValueError("expected an absolute guest path")
    return result


def _integer(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("expected a positive integer")
    return value


def _optional_integer(value: Any) -> int | None:
    return None if value is None else _integer(value)


def _port(value: Any) -> int:
    result = _integer(value)
    if result > 65535:
        raise ValueError("port must be in 1..65535")
    return result


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError("expected a boolean")
    return value

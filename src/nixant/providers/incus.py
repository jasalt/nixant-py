"""Incus CLI adapter. Every instance and API request uses the local remote."""

import json
import re
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

from nixant.errors import CommandError, NixantError
from nixant.models import MachineSpec, MachineState, MountSpec, Snapshot
from nixant.planner import MOUNT_PREFIX, PORT_PREFIX, Change
from nixant.run import Runner

# Polls of /proc/mounts, 0.5s apart, before a missing mount is re-added.
MOUNT_POLLS = 20


def _local(name: str) -> str:
    if not name or any(char in name for char in "/:\0\n"):
        raise NixantError(f"invalid instance name: {name!r}")
    return f"local:{name}"


def _state(data: Any) -> MachineState:
    try:
        network = (data.get("state") or {}).get("network") or {}
        return MachineState(
            name=data["name"],
            status=data["status"],
            # Incus says "virtual-machine"; nixant uses "vm" everywhere else.
            kind="vm" if data["type"] == "virtual-machine" else data["type"],
            config=data["config"],
            devices=data["devices"],
            created_at=data.get("created_at", ""),
            ephemeral=bool(data.get("ephemeral", False)),
            expanded_devices=data.get("expanded_devices") or {},
            ipv4=tuple(
                address["address"]
                for interface in network.values()
                for address in interface.get("addresses", [])
                if address.get("family") == "inet" and address.get("scope") == "global"
            ),
        )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise NixantError(f"invalid Incus instance response: {exc}") from exc


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise NixantError(f"invalid Incus JSON response: {exc}") from exc


class IncusProvider:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def inspect(self, name: str) -> MachineState | None:
        _local(name)
        argv = [
            "incus",
            "query",
            f"local:/1.0/instances/{quote(name, safe='')}?recursion=1",
        ]
        result = self.runner.run(argv, capture=True, capture_stderr=True, check=False)
        if result.returncode:
            # Do not conflate daemon/access errors with a missing instance.
            if b"Instance not found" in (result.stderr or b""):
                return None
            raise CommandError(argv, result.returncode, result.stderr)
        return _state(_json(result.stdout))

    def find(self, metadata: Mapping[str, str]) -> list[MachineState]:
        result = self.runner.run(
            [
                "incus",
                "list",
                "local:",
                *[f"{k}={v}" for k, v in sorted(metadata.items())],
                "--format",
                "json",
            ],
            capture=True,
        )
        data = _json(result.stdout)
        if not isinstance(data, list):
            raise NixantError("invalid Incus list response: expected an array")
        return [_state(item) for item in data]

    def create(self, spec: MachineSpec, metadata: Mapping[str, str]) -> None:
        # Validate mounts before creating an instance, including direct API callers.
        for mount in spec.mounts:
            self._mount_args(mount)
        argv = [
            "incus",
            "create",
            "images:nixos/unstable",
            _local(spec.instance_name),
            *(["--ephemeral"] if spec.ephemeral else []),
            # Nesting lets guest-side Nix sandbox builds work in containers; the
            # stock VM image declares secureboot incompatible.
            *(
                ["--vm", "-c", "security.secureboot=false"]
                if spec.kind == "vm"
                else ["-c", "security.nesting=true"]
            ),
        ]
        for key, value in sorted(metadata.items()):
            argv.extend(["-c", f"{key}={value}"])
        if spec.disk_bytes is not None:
            # An instance-local root override; the pool is inherited from the
            # profile. Creating at the profile size first would make a smaller
            # requested size look like a shrink.
            argv.extend(["-d", f"root,size={spec.disk_bytes}"])
        self.runner.run(argv)
        for mount in spec.mounts:
            self.ensure_mount(spec.instance_name, mount)

    def start(self, name: str) -> None:
        self.runner.run(["incus", "start", _local(name)])

    def stop(self, name: str, *, force: bool = False) -> None:
        self.runner.run(
            [
                "incus",
                "stop",
                _local(name),
                *(["--force"] if force else ["--timeout", "60"]),
            ]
        )

    def restart(
        self, name: str, *, force: bool = False, timeout: float | None = None
    ) -> None:
        self.runner.run(
            [
                "incus",
                "restart",
                _local(name),
                *(["--force"] if force else ["--timeout", "60"]),
            ],
            timeout=timeout,
        )

    def destroy(self, name: str) -> None:
        self.runner.run(["incus", "delete", "--force", _local(name)])

    def set_metadata(self, name: str, metadata: Mapping[str, str]) -> None:
        if any(not key.startswith("user.nixant.") for key in metadata):
            raise NixantError("refusing to write non-nixant metadata")
        if metadata:
            self.runner.run(
                [
                    "incus",
                    "config",
                    "set",
                    _local(name),
                    *[f"{k}={v}" for k, v in sorted(metadata.items())],
                ]
            )

    @staticmethod
    def _mount_args(mount: MountSpec) -> list[str]:
        if not mount.name or "/" in mount.name or "\0" in mount.name:
            raise NixantError("invalid mount name")
        if not Path(mount.source).is_absolute() or not Path(mount.source).exists():
            raise NixantError(
                f"mount {mount.name} requires an existing absolute source"
            )
        return [
            f"source={mount.source}",
            f"path={mount.target}",
            "shift=true",
            f"readonly={'true' if mount.read_only else 'false'}",
        ]

    def apply(self, name: str, change: Change) -> None:
        """Run one planned change; the planner has already decided it is live."""
        target = _local(name)
        values = [f"{key}={value}" for key, value in change.values.items()]
        if change.op == "config":
            self.runner.run(
                [
                    "incus",
                    "config",
                    "set",
                    target,
                    f"{change.key}={change.values['value']}",
                ]
            )
        elif change.op == "device-add":
            kind = "proxy" if change.key.startswith(PORT_PREFIX) else "disk"
            self.runner.run(
                ["incus", "config", "device", "add", target, change.key, kind, *values]
            )
        elif change.op == "device-set":
            self.runner.run(
                ["incus", "config", "device", "set", target, change.key, *values]
            )
        elif change.op == "device-remove":
            self.runner.run(["incus", "config", "device", "remove", target, change.key])
        elif change.op == "root-size":
            # Only the size key of the instance-local root device is ever touched.
            verb = "set" if change.values.get("local") else "override"
            self.runner.run(
                [
                    "incus",
                    "config",
                    "device",
                    verb,
                    target,
                    "root",
                    f"size={change.values['size']}",
                ]
            )
        else:
            raise NixantError(f"unknown change operation {change.op!r}")

    def check_quota(self, pool: str | None) -> None:
        """Fail before applying a disk size the root pool cannot enforce."""
        if pool is None:
            profile = _json(
                self.runner.run(
                    ["incus", "query", "local:/1.0/profiles/default"], capture=True
                ).stdout
            )
            pool = (profile.get("devices") or {}).get("root", {}).get("pool")
        if not isinstance(pool, str) or not pool:
            raise NixantError("cannot determine the root storage pool for nixant.disk")
        info = _json(
            self.runner.run(
                ["incus", "query", f"local:/1.0/storage-pools/{quote(pool, safe='')}"],
                capture=True,
            ).stdout
        )
        driver = info.get("driver") if isinstance(info, dict) else None
        if driver == "dir":
            raise NixantError(
                f"nixant.disk is not supported on storage pool {pool} (driver dir); "
                "unset it or use a btrfs/zfs/lvm pool"
            )

    def snapshot_create(self, name: str, snapshot: str) -> None:
        self.runner.run(["incus", "snapshot", "create", _local(name), snapshot])

    def snapshot_list(self, name: str) -> list[Snapshot]:
        _local(name)
        result = self.runner.run(
            [
                "incus",
                "query",
                f"local:/1.0/instances/{quote(name, safe='')}/snapshots?recursion=1",
            ],
            capture=True,
        )
        data = _json(result.stdout)
        if not isinstance(data, list):
            raise NixantError("invalid Incus snapshot response: expected an array")
        try:
            return sorted(
                (
                    Snapshot(
                        name=str(item["name"]),
                        created_at=str(item.get("created_at", "")),
                        stateful=bool(item.get("stateful", False)),
                        config=item.get("config") or {},
                        devices=item.get("devices") or {},
                    )
                    for item in data
                ),
                key=lambda snap: (snap.created_at, snap.name),
            )
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise NixantError(f"invalid Incus snapshot response: {exc}") from exc

    def snapshot_delete(self, name: str, snapshot: str) -> None:
        self.runner.run(["incus", "snapshot", "delete", _local(name), snapshot])

    def snapshot_restore(self, name: str, snapshot: str) -> None:
        self.runner.run(["incus", "snapshot", "restore", _local(name), snapshot])

    def ensure_mount(
        self, name: str, mount: MountSpec, *, verify: bool = False
    ) -> None:
        args = self._mount_args(mount)
        device = f"{MOUNT_PREFIX}{mount.name}"
        state = self.inspect(name)
        if state is None:
            raise NixantError(f"instance {name} disappeared")
        desired = dict(item.split("=", 1) for item in args)
        actual = state.devices.get(device)
        if actual is not None:
            if actual.get("type") != "disk":
                raise NixantError(f"{device} exists but is not a disk device")
            if any(
                actual.get(key, "false" if key == "readonly" else "") != value
                for key, value in desired.items()
            ):
                self.runner.run(
                    ["incus", "config", "device", "set", _local(name), device, *args]
                )
        else:
            self.runner.run(
                [
                    "incus",
                    "config",
                    "device",
                    "add",
                    _local(name),
                    device,
                    "disk",
                    *args,
                ]
            )
        if not verify:
            return
        for attempt in range(3):
            if self._mounted(name, mount.target):
                return
            if attempt < 2:
                self.runner.run(
                    ["incus", "config", "device", "remove", _local(name), device]
                )
                self.runner.run(
                    [
                        "incus",
                        "config",
                        "device",
                        "add",
                        _local(name),
                        device,
                        "disk",
                        *args,
                    ]
                )
        raise NixantError(
            f"mount {mount.name} did not appear at {mount.target} in {name}"
        )

    def _mounted(self, name: str, target: str) -> bool:
        """Wait for target to be mounted; a VM re-plugs virtiofs asynchronously."""
        for poll in range(MOUNT_POLLS):
            result = self.run(name, ["cat", "/proc/mounts"], capture=True)
            paths = [
                re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[1])
                for line in result.stdout.decode().splitlines()
                if len(line.split()) >= 2
            ]
            if target in paths:
                return True
            if poll < MOUNT_POLLS - 1:
                time.sleep(0.5)
        return False

    def exec_argv(
        self, name: str, argv: list[str], *, user: str, cwd: str
    ) -> list[str]:
        return [
            "incus",
            "exec",
            _local(name),
            "--cwd",
            cwd,
            "--",
            "/run/current-system/sw/bin/runuser",
            "-u",
            user,
            "--",
            "/run/current-system/sw/bin/bash",
            "-lc",
            'exec "$@"',
            "nixant",
            *argv,
        ]

    def run(
        self,
        name: str,
        argv: list[str],
        *,
        user: str | None = None,
        cwd: str | None = None,
        stdin: BinaryIO | None = None,
        capture: bool = False,
        capture_stderr: bool = False,
        tee_stderr: bool = False,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if user is not None:
            args = self.exec_argv(name, argv, user=user, cwd=cwd or "/")
        else:
            args = ["incus", "exec", _local(name)]
            if cwd is not None:
                args.extend(["--cwd", cwd])
            args.extend(["--", *argv])
        # Programmatic execution must never allocate a pseudo-terminal (NAR data).
        args.insert(2, "-T")
        return self.runner.run(
            args,
            stdin=stdin,
            capture=capture,
            capture_stderr=capture_stderr,
            tee_stderr=tee_stderr,
            check=check,
            timeout=timeout,
        )

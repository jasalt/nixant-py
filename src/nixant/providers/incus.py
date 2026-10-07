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
from nixant.models import MachineSpec, MachineState, MountSpec
from nixant.run import Runner

MOUNT_PREFIX = "nixant-mount-"


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
            kind=data["type"],
            config=data["config"],
            devices=data["devices"],
            created_at=data.get("created_at", ""),
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
        if spec.kind != "container":
            raise NixantError("VM creation is not supported yet")
        # Validate mounts before creating an instance, including direct API callers.
        for mount in spec.mounts:
            self._mount_args(mount)
        argv = [
            "incus",
            "create",
            "images:nixos/unstable",
            _local(spec.instance_name),
            "-c",
            "security.nesting=true",
        ]
        for key, value in sorted(metadata.items()):
            argv.extend(["-c", f"{key}={value}"])
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

    def restart(self, name: str, *, force: bool = False) -> None:
        self.runner.run(
            [
                "incus",
                "restart",
                _local(name),
                *(["--force"] if force else ["--timeout", "60"]),
            ]
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

    def remove_mount(self, name: str, mount_name: str) -> None:
        self.runner.run(
            [
                "incus",
                "config",
                "device",
                "remove",
                _local(name),
                f"{MOUNT_PREFIX}{mount_name}",
            ]
        )

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
            result = self.run(name, ["cat", "/proc/mounts"], capture=True)
            paths = [
                re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[1])
                for line in result.stdout.decode().splitlines()
                if len(line.split()) >= 2
            ]
            if mount.target in paths:
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
                time.sleep(0.2)
        raise NixantError(
            f"mount {mount.name} did not appear at {mount.target} in {name}"
        )

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

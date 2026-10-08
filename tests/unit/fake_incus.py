"""An in-memory Incus for unit tests, behind the real IncusProvider."""

import copy
import json
import subprocess
from pathlib import Path

from nixant.errors import CommandError


class FakeIncus:
    """An in-memory Incus that enforces the rules nixant must work within."""

    def __init__(self, data: dict | None, profile_root: dict) -> None:
        self.data = data
        self.profile_root = profile_root
        self.calls: list[list[str]] = []
        self.fail: dict[str, bytes] = {}
        self.snapshots: dict[str, dict] = {}

    def response(self) -> dict:
        assert self.data is not None
        root = {**self.profile_root, **self.data["devices"].get("root", {})}
        return {**self.data, "expanded_devices": {**self.data["devices"], "root": root}}

    def claim_path(self, device: str, path: str | None) -> None:
        # Incus refuses two disk devices mounted at the same guest path.
        assert self.data is not None
        for other, existing in self.data["devices"].items():
            if other != device and path and existing.get("path") == path:
                raise CommandError(["incus"], 1, b"path is already in use")

    def create(self, argv: list[str]) -> None:
        options = list(zip(argv, argv[1:], strict=False))
        devices = {}
        for flag, value in options:
            if flag == "-d":
                device, *items = value.split(",")
                devices[device] = dict(item.split("=", 1) for item in items)
        self.data = {
            "name": argv[3].removeprefix("local:"),
            "status": "Stopped",
            "type": "virtual-machine" if "--vm" in argv else "container",
            "ephemeral": "--ephemeral" in argv,
            "config": dict(v.split("=", 1) for f, v in options if f == "-c"),
            "devices": devices,
        }

    def start(self, argv: list[str]) -> None:
        assert self.data is not None
        for device in self.data["devices"].values():
            source = device.get("source")
            if device.get("type") == "disk" and source and not Path(source).exists():
                raise CommandError(argv, 1, b"Missing source path " + source.encode())
        self.data["status"] = "Running"

    def stop(self) -> None:
        assert self.data is not None
        if self.data.get("ephemeral"):
            self.data = None
        else:
            self.data["status"] = "Stopped"

    def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        for prefix, stderr in self.fail.items():
            if " ".join(argv).startswith(prefix):
                raise CommandError(argv, 1, stderr)
        stdout = b""
        if argv[:2] == ["incus", "create"]:
            self.create(argv)
        elif argv[:3] == ["incus", "list", "local:"]:
            # Filters are config key=value pairs, ANDed, as nixant passes them.
            filters = dict(item.split("=", 1) for item in argv[3:-2])
            found = self.data is not None and all(
                self.data["config"].get(key) == value for key, value in filters.items()
            )
            stdout = json.dumps([self.response()] if found else []).encode()
        elif argv[:3] == ["incus", "query", "local:/1.0/profiles/default"]:
            stdout = json.dumps({"devices": {"root": self.profile_root}}).encode()
        elif argv[:3] == ["incus", "query", "local:/1.0/storage-pools/default"]:
            stdout = json.dumps({"driver": "btrfs"}).encode()
        elif argv[:2] == ["incus", "query"] and argv[2].endswith(
            "/snapshots?recursion=1"
        ):
            stdout = json.dumps(list(self.snapshots.values())).encode()
        elif argv[:2] == ["incus", "query"]:
            if self.data is None:
                return subprocess.CompletedProcess(
                    argv, 1, b"", b"Error: Instance not found"
                )
            stdout = json.dumps(self.response()).encode()
        elif self.data is None:
            raise RuntimeError(f"no instance for {argv}")
        elif argv[:3] == ["incus", "config", "set"]:
            self.data["config"].update(item.split("=", 1) for item in argv[4:])
        elif argv[:4] == ["incus", "config", "device", "remove"]:
            del self.data["devices"][argv[5]]
        elif argv[:4] == ["incus", "config", "device", "add"]:
            if argv[5] in self.data["devices"]:
                raise CommandError(argv, 1, b"device already exists")
            device = {"type": argv[6], **dict(i.split("=", 1) for i in argv[7:])}
            self.claim_path(argv[5], device.get("path"))
            self.data["devices"][argv[5]] = device
        elif argv[:4] == ["incus", "config", "device", "set"]:
            values = dict(item.split("=", 1) for item in argv[6:])
            self.claim_path(argv[5], values.get("path"))
            self.data["devices"][argv[5]].update(values)
        elif argv[:4] == ["incus", "config", "device", "override"]:
            root = self.data["devices"].setdefault("root", {})
            root.update(item.split("=", 1) for item in argv[6:])
        elif argv[:2] == ["incus", "start"]:
            self.start(argv)
        elif argv[:2] == ["incus", "stop"]:
            self.stop()
        elif argv[:3] == ["incus", "snapshot", "create"]:
            self.snapshots[argv[4]] = copy.deepcopy(
                {
                    "name": argv[4],
                    "config": self.data["config"],
                    "devices": self.data["devices"],
                }
            )
        elif argv[:3] == ["incus", "snapshot", "restore"]:
            # Incus stops a running instance, restores, and starts it again.
            running = self.data["status"] == "Running"
            self.data["status"] = "Stopped"
            saved = copy.deepcopy(self.snapshots[argv[4]])
            self.data["config"], self.data["devices"] = (
                saved["config"],
                saved["devices"],
            )
            if running:
                self.start(argv)
        elif argv[:3] == ["incus", "exec", "-T"]:
            command = argv[argv.index("--") + 1 :]
            if command == ["readlink", "-f", "/run/current-system"]:
                stdout = b"system\n"
            elif command == ["cat", "/proc/mounts"]:
                stdout = "".join(
                    f"source {device['path']} none rw 0 0\n"
                    for device in self.data["devices"].values()
                    if device.get("path")
                ).encode()
            else:
                raise RuntimeError(f"unexpected guest command: {argv}")
        else:
            raise RuntimeError(f"unexpected Incus command: {argv}")
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

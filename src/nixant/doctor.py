"""Host requirements, checked without a project or a Nix evaluation."""

import grp
import json
import os
import platform
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from nixant.deploy import render_node, wayland_socket
from nixant.errors import NixantError
from nixant.run import Runner

Status = Literal["ok", "warn", "fail"]
NIX_DAEMON_SOCKET = Path("/nix/var/nix/daemon-socket/socket")


@dataclass(frozen=True)
class Check:
    status: Status
    name: str
    detail: str


def checks(runner: Runner) -> Iterator[Check]:
    yield _platform()
    missing = [tool for tool in ("incus", "nix", "git") if shutil.which(tool) is None]
    yield (
        Check("fail", "tools", f"not found on PATH: {', '.join(missing)}")
        if missing
        else Check("ok", "tools", "incus, nix and git on PATH")
    )
    yield _incus_admin()
    if "incus" not in missing:
        yield from _incus(runner)
    if "nix" not in missing:
        yield from _nix(runner)
    yield _optional("caddy", shutil.which("caddy"), "nixant proxy")
    socket = wayland_socket()
    live = socket if socket and Path(socket).is_socket() else None
    yield _optional("wayland", live, "nixant.wayland")
    yield _optional("gpu", render_node(), "nixant.gpu")


def _platform() -> Check:
    system, machine = platform.system(), platform.machine()
    if (system, machine) == ("Linux", "x86_64"):
        return Check("ok", "platform", "Linux x86_64")
    return Check("fail", "platform", f"{system} {machine}; nixant needs Linux x86_64")


def _incus_admin() -> Check:
    try:
        gid = grp.getgrnam("incus-admin").gr_gid
    except KeyError:
        return Check("fail", "incus-admin", "the group does not exist; install Incus")
    if gid in os.getgroups() or os.getuid() == 0:
        return Check("ok", "incus-admin", "your user is a member")
    return Check(
        "fail",
        "incus-admin",
        "your user is not a member (or a new login is needed after adding it)",
    )


def _query(runner: Runner, path: str) -> Any:
    result = runner.run(
        ["incus", "query", f"local:{path}"],
        capture=True,
        capture_stderr=True,
        check=False,
    )
    if result.returncode:
        message = (result.stderr or b"").decode(errors="replace").strip()
        raise NixantError(message or f"incus query exited with {result.returncode}")
    return json.loads(result.stdout)


def _incus(runner: Runner) -> Iterator[Check]:
    try:
        server = _query(runner, "/1.0")
    except (NixantError, ValueError) as exc:
        yield Check("fail", "incus", f"the local daemon does not answer: {exc}")
        return
    environment = server.get("environment", {})
    yield Check(
        "ok",
        "incus",
        f"Incus {environment.get('server_version', '?')}, "
        f"storage {environment.get('storage', '?')}",
    )
    idmapped = environment.get("kernel_features", {}).get("idmapped_mounts")
    if idmapped == "false":
        yield Check(
            "fail",
            "idmapped mounts",
            "not supported; the shifted workspace mount needs them",
        )
    elif idmapped == "true":
        yield Check("ok", "idmapped mounts", "supported")
    else:
        yield Check(
            "ok", "idmapped mounts", "not reported by Incus; up checks the mount"
        )
    try:
        profile = _query(runner, "/1.0/profiles/default")
    except (NixantError, ValueError) as exc:
        yield Check("fail", "default profile", f"cannot read it: {exc}")
        return
    devices = profile.get("devices", {}).values()
    root = any(d.get("type") == "disk" and d.get("path") == "/" for d in devices)
    nic = any(d.get("type") == "nic" for d in devices)
    if root and nic:
        yield Check("ok", "default profile", "root disk and NIC")
    else:
        lacking = [
            what for what, ok in (("a root disk", root), ("a NIC", nic)) if not ok
        ]
        yield Check(
            "fail",
            "default profile",
            f"lacks {' and '.join(lacking)}; see incus admin init",
        )


def _nix(runner: Runner) -> Iterator[Check]:
    if NIX_DAEMON_SOCKET.exists():
        yield Check("ok", "nix daemon", "multi-user Nix")
    else:
        yield Check(
            "fail", "nix daemon", f"no {NIX_DAEMON_SOCKET}; install multi-user Nix"
        )
    result = runner.run(
        ["nix", "config", "show", "experimental-features"],
        capture=True,
        capture_stderr=True,
        check=False,
    )
    features = result.stdout.decode().split() if result.returncode == 0 else []
    absent = [f for f in ("nix-command", "flakes") if f not in features]
    if absent:
        yield Check(
            "fail",
            "flakes",
            f"enable {' and '.join(absent)} in experimental-features (nix.conf)",
        )
    else:
        yield Check("ok", "flakes", "nix-command and flakes enabled")


def _optional(name: str, found: str | None, needed_by: str) -> Check:
    if found:
        return Check("ok", name, found)
    return Check("warn", name, f"not available; only {needed_by} needs it")


def report(found: Iterator[Check], echo: Callable[[str], None]) -> bool:
    """Print every check; True when none failed."""
    failed = False
    for check in found:
        echo(f"{check.status:<4}  {check.name}: {check.detail}")
        failed = failed or check.status == "fail"
    return not failed

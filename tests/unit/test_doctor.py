import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nixant import doctor
from nixant.cli import app

SERVER = {"environment": {"server_version": "7.4", "storage": "btrfs"}}
PROFILE = {
    "devices": {
        "root": {"type": "disk", "path": "/", "pool": "default"},
        "eth0": {"type": "nic", "network": "incusbr0"},
    }
}


def done(stdout: object, rc: int = 0) -> subprocess.CompletedProcess[bytes]:
    text = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return subprocess.CompletedProcess([], rc, text.encode(), b"boom" if rc else b"")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """A host that passes every check; tests break one thing at a time."""
    socket = tmp_path / "nix-daemon"
    socket.touch()
    monkeypatch.setattr(doctor, "NIX_DAEMON_SOCKET", socket)
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
    monkeypatch.setattr(doctor.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: f"/bin/{tool}")
    monkeypatch.setattr(doctor.grp, "getgrnam", lambda name: Mock(gr_gid=977))
    monkeypatch.setattr(doctor.os, "getgroups", lambda: [1000, 977])
    monkeypatch.setattr(doctor, "wayland_socket", lambda: None)
    monkeypatch.setattr(doctor, "render_node", lambda: "/dev/dri/renderD128")
    answers = {
        "local:/1.0": done(SERVER),
        "local:/1.0/profiles/default": done(PROFILE),
        "experimental-features": done("fetch-tree flakes nix-command\n"),
    }
    runner = Mock()
    runner.run.side_effect = lambda argv, **kwargs: answers[argv[-1]]
    monkeypatch.setattr("nixant.cli.Runner", lambda verbose: runner)
    return answers


def run() -> tuple[int, str]:
    result = CliRunner().invoke(app, ["doctor"])
    return result.exit_code, result.output


def test_healthy_host(host: dict[str, object]) -> None:
    code, output = run()
    assert code == 0, output
    assert "ok    incus: Incus 7.4, storage btrfs" in output
    assert "ok    gpu: /dev/dri/renderD128" in output
    assert "warn  wayland: not available; only nixant.wayland needs it" in output
    assert "fail" not in output


def test_missing_tools_are_reported_not_raised(
    host: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        doctor.shutil, "which", lambda tool: None if tool == "incus" else "/bin/x"
    )
    code, output = run()
    assert code == 1
    assert "fail  tools: not found on PATH: incus" in output
    assert "incus:" not in output.replace("PATH: incus", "")


def test_not_in_incus_admin(
    host: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor.os, "getgroups", lambda: [1000])
    monkeypatch.setattr(doctor.os, "getuid", lambda: 1000)
    code, output = run()
    assert code == 1
    assert "fail  incus-admin: your user is not a member" in output


def test_daemon_down_skips_dependent_checks(host: dict[str, object]) -> None:
    host["local:/1.0"] = done("", rc=1)
    code, output = run()
    assert code == 1
    assert "fail  incus: the local daemon does not answer: boom" in output
    assert "default profile" not in output


def test_profile_without_nic_and_no_flakes(host: dict[str, object]) -> None:
    host["local:/1.0/profiles/default"] = done(
        {"devices": {"root": {"type": "disk", "path": "/"}}}
    )
    host["experimental-features"] = done("nix-command\n")
    code, output = run()
    assert code == 1
    assert "fail  default profile: lacks a NIC; see incus admin init" in output
    assert "fail  flakes: enable flakes" in output


def test_idmapped_mounts_reported_false(host: dict[str, object]) -> None:
    host["local:/1.0"] = done(
        {"environment": {"kernel_features": {"idmapped_mounts": "false"}}}
    )
    code, output = run()
    assert code == 1
    assert "fail  idmapped mounts: not supported" in output

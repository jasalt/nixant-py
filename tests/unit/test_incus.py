import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from nixant.errors import CommandError, NixantError
from nixant.models import MachineSpec, MachineState, MountSpec
from nixant.providers.incus import IncusProvider
from nixant.run import Runner


def response(
    data: object, rc: int = 0, stderr: bytes = b""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, json.dumps(data).encode(), stderr)


@pytest.fixture
def provider() -> IncusProvider:
    return IncusProvider(Mock(spec=Runner))


def test_inspect(provider: IncusProvider) -> None:
    provider.runner.run.return_value = response(
        {
            "name": "dev",
            "type": "container",
            "status": "Running",
            "config": {"user.nixant.managed": "true"},
            "devices": {},
            "state": {
                "network": {
                    "eth0": {
                        "addresses": [
                            {
                                "family": "inet",
                                "scope": "global",
                                "address": "10.0.0.2",
                            },
                            {
                                "family": "inet",
                                "scope": "local",
                                "address": "127.0.0.1",
                            },
                        ]
                    }
                }
            },
        }
    )
    state = provider.inspect("dev")
    assert state.ipv4 == ("10.0.0.2",)
    provider.runner.run.assert_called_once_with(
        ["incus", "query", "local:/1.0/instances/dev?recursion=1"],
        capture=True,
        capture_stderr=True,
        check=False,
    )
    with pytest.raises(TypeError):
        state.config["new"] = "value"


def test_absent_vs_daemon_error(provider: IncusProvider) -> None:
    provider.runner.run.return_value = response(None, 1, b"Error: Instance not found")
    assert provider.inspect("dev") is None
    provider.runner.run.return_value = response(None, 1, b"Error: permission denied")
    with pytest.raises(CommandError, match="permission denied"):
        provider.inspect("dev")


def test_find(provider: IncusProvider) -> None:
    provider.runner.run.return_value = response([])
    assert (
        provider.find({"user.nixant.target": "dev", "user.nixant.project": "abc"}) == []
    )
    assert provider.runner.run.call_args.args[0] == [
        "incus",
        "list",
        "local:",
        "user.nixant.project=abc",
        "user.nixant.target=dev",
        "--format",
        "json",
    ]


def test_create(provider: IncusProvider) -> None:
    data = json.loads(
        (Path(__file__).parents[2] / "nix/tests/runtime.json").read_text()
    )
    spec = replace(MachineSpec.from_runtime(data), mounts=())
    provider.create(spec, {"user.nixant.managed": "true"})
    assert provider.runner.run.call_args.args[0] == [
        "incus",
        "create",
        "images:nixos/unstable",
        "local:test-dev",
        "-c",
        "security.nesting=true",
        "-c",
        "user.nixant.managed=true",
    ]


def test_lifecycle(provider: IncusProvider) -> None:
    provider.start("dev")
    provider.stop("dev")
    provider.stop("dev", force=True)
    provider.restart("dev")
    provider.destroy("dev")
    assert [c.args[0] for c in provider.runner.run.call_args_list] == [
        ["incus", "start", "local:dev"],
        ["incus", "stop", "local:dev", "--timeout", "60"],
        ["incus", "stop", "local:dev", "--force"],
        ["incus", "restart", "local:dev", "--timeout", "60"],
        ["incus", "delete", "--force", "local:dev"],
    ]


def test_exec_and_binary_run(provider: IncusProvider, tmp_path: Path) -> None:
    command = ["printf", "%s", "a; b", "--flag"]
    args = provider.exec_argv("dev", command, user="dev", cwd="/workspace")
    assert args == [
        "incus",
        "exec",
        "local:dev",
        "--cwd",
        "/workspace",
        "--",
        "/run/current-system/sw/bin/runuser",
        "-u",
        "dev",
        "--",
        "/run/current-system/sw/bin/bash",
        "-lc",
        'exec "$@"',
        "nixant",
        *command,
    ]
    with (tmp_path / "input").open("w+b") as stream:
        provider.run("dev", ["nix-store", "--import"], stdin=stream, timeout=30)
        call = provider.runner.run.call_args
        assert call.args[0] == [
            "incus",
            "exec",
            "-T",
            "local:dev",
            "--",
            "nix-store",
            "--import",
        ]
        assert call.kwargs["stdin"] is stream
        assert call.kwargs["timeout"] == 30


def test_mount_retry(
    provider: IncusProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nixant.providers.incus.time.sleep", lambda _: None)
    provider.inspect = Mock(
        return_value=MachineState("dev", "Running", "container", {}, {})
    )
    provider.run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, b""),
            subprocess.CompletedProcess(
                [], 0, b"source /work\\040space virtiofs rw 0 0\n"
            ),
        ]
    )
    provider.ensure_mount(
        "dev", MountSpec("workspace", str(tmp_path), "/work space"), verify=True
    )
    calls = [c.args[0] for c in provider.runner.run.call_args_list]
    assert len(calls) == 3
    assert calls[0][-2:] == ["shift=true", "readonly=false"]
    assert calls[1] == [
        "incus",
        "config",
        "device",
        "remove",
        "local:dev",
        "nixant-mount-workspace",
    ]
    assert calls[0] == calls[2]


def test_mount_retry_bounded(
    provider: IncusProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nixant.providers.incus.time.sleep", lambda _: None)
    provider.inspect = Mock(
        return_value=MachineState("dev", "Running", "container", {}, {})
    )
    provider.run = Mock(return_value=subprocess.CompletedProcess([], 0, b""))
    with pytest.raises(NixantError, match="did not appear"):
        provider.ensure_mount(
            "dev", MountSpec("workspace", str(tmp_path), "/workspace"), verify=True
        )
    assert provider.run.call_count == 3


def test_shift_failure_never_retries_unshifted(
    provider: IncusProvider, tmp_path: Path
) -> None:
    provider.inspect = Mock(
        return_value=MachineState("dev", "Running", "container", {}, {})
    )
    provider.runner.run.side_effect = CommandError(["incus"], 1, b"shift unsupported")
    with pytest.raises(CommandError, match="shift unsupported"):
        provider.ensure_mount(
            "dev", MountSpec("workspace", str(tmp_path), "/workspace")
        )
    assert provider.runner.run.call_count == 1
    assert "shift=true" in provider.runner.run.call_args.args[0]


def test_metadata_scope(provider: IncusProvider) -> None:
    with pytest.raises(NixantError, match="non-nixant"):
        provider.set_metadata("dev", {"security.privileged": "true"})
    provider.runner.run.assert_not_called()

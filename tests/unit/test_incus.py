import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from nixant.errors import CommandError, NixantError
from nixant.models import MachineSpec, MachineState, MountSpec
from nixant.planner import Change, Effect
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


def test_create_vm(provider: IncusProvider) -> None:
    data = json.loads(
        (Path(__file__).parents[2] / "nix/tests/runtime.json").read_text()
    )
    spec = replace(MachineSpec.from_runtime(data), kind="vm", mounts=())
    provider.create(spec, {"user.nixant.managed": "true"})
    assert provider.runner.run.call_args.args[0] == [
        "incus",
        "create",
        "images:nixos/unstable",
        "local:test-dev",
        "--vm",
        "-c",
        "security.secureboot=false",
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


@pytest.mark.parametrize("omit_readonly", [False, True])
def test_matching_mount_is_not_mutated(
    provider: IncusProvider, tmp_path: Path, omit_readonly: bool
) -> None:
    device = {
        "type": "disk",
        "source": str(tmp_path),
        "path": "/workspace",
        "shift": "true",
    }
    if not omit_readonly:
        device["readonly"] = "false"
    provider.inspect = Mock(
        return_value=MachineState(
            "dev", "Running", "container", {}, {"nixant-mount-workspace": device}
        )
    )
    provider.run = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, b"source /workspace none rw 0 0\n"
        )
    )
    provider.ensure_mount(
        "dev", MountSpec("workspace", str(tmp_path), "/workspace"), verify=True
    )
    provider.runner.run.assert_not_called()
    provider.run.assert_called_once_with("dev", ["cat", "/proc/mounts"], capture=True)


@pytest.mark.parametrize(
    ("key", "old_value"),
    [
        ("source", "/old-checkout"),
        ("path", "/old-workspace"),
        ("readonly", "true"),
        ("shift", "false"),
    ],
)
def test_changed_mount_updates_existing_device(
    provider: IncusProvider, tmp_path: Path, key: str, old_value: str
) -> None:
    device = {
        "type": "disk",
        "source": str(tmp_path),
        "path": "/workspace",
        "readonly": "false",
        "shift": "true",
    }
    device[key] = old_value
    provider.inspect = Mock(
        return_value=MachineState(
            "dev", "Running", "container", {}, {"nixant-mount-workspace": device}
        )
    )
    provider.ensure_mount("dev", MountSpec("workspace", str(tmp_path), "/workspace"))
    provider.runner.run.assert_called_once_with(
        [
            "incus",
            "config",
            "device",
            "set",
            "local:dev",
            "nixant-mount-workspace",
            f"source={tmp_path}",
            "path=/workspace",
            "shift=true",
            "readonly=false",
        ]
    )


@pytest.mark.parametrize("missing", [False, True])
def test_mount_rejects_missing_instance_or_wrong_device_type(
    provider: IncusProvider, tmp_path: Path, missing: bool
) -> None:
    provider.inspect = Mock(
        return_value=None
        if missing
        else MachineState(
            "dev",
            "Running",
            "container",
            {},
            {"nixant-mount-workspace": {"type": "nic"}},
        )
    )
    with pytest.raises(
        NixantError, match="disappeared" if missing else "not a disk device"
    ):
        provider.ensure_mount(
            "dev", MountSpec("workspace", str(tmp_path), "/workspace")
        )
    provider.runner.run.assert_not_called()


@pytest.mark.parametrize("source", ["relative", "/nonexistent/nixant-mount-test"])
def test_create_validates_mounts_before_mutation(
    provider: IncusProvider, source: str
) -> None:
    spec = MachineSpec.from_runtime(
        json.loads((Path(__file__).parents[2] / "nix/tests/runtime.json").read_text())
    )
    spec = replace(spec, mounts=(MountSpec("workspace", source, "/workspace"),))
    with pytest.raises(NixantError, match="existing absolute source"):
        provider.create(spec, {"user.nixant.managed": "true"})
    provider.runner.run.assert_not_called()


def test_set_metadata_uses_one_literal_command(provider: IncusProvider) -> None:
    provider.set_metadata(
        "dev",
        {
            "user.nixant.workdir": "/work space; $(false)",
            "user.nixant.activation": "ok",
        },
    )
    provider.runner.run.assert_called_once_with(
        [
            "incus",
            "config",
            "set",
            "local:dev",
            "user.nixant.activation=ok",
            "user.nixant.workdir=/work space; $(false)",
        ]
    )


def test_empty_metadata_does_not_run_command(provider: IncusProvider) -> None:
    provider.set_metadata("dev", {})
    provider.runner.run.assert_not_called()


@pytest.mark.parametrize("raw", [b"not JSON", b"null", b"{}", b"[]"])
def test_inspect_rejects_malformed_responses(
    provider: IncusProvider, raw: bytes
) -> None:
    provider.runner.run.return_value = subprocess.CompletedProcess([], 0, raw)
    with pytest.raises(NixantError, match="invalid Incus"):
        provider.inspect("dev")


@pytest.mark.parametrize("data", [{}, None, [{}]])
def test_find_rejects_malformed_responses(
    provider: IncusProvider, data: object
) -> None:
    provider.runner.run.return_value = response(data)
    with pytest.raises(NixantError, match="invalid Incus"):
        provider.find({"user.nixant.managed": "true"})


@pytest.mark.parametrize(
    ("change", "argv"),
    [
        (
            Change("cpus", Effect.LIVE, "", "config", "limits.cpu", {"value": "2"}),
            ["config", "set", "local:dev", "limits.cpu=2"],
        ),
        (
            Change(
                "ports",
                Effect.LIVE,
                "",
                "device-add",
                "nixant-port-8080",
                {"listen": "tcp:127.0.0.1:8080", "connect": "tcp:127.0.0.1:80"},
            ),
            [
                "config",
                "device",
                "add",
                "local:dev",
                "nixant-port-8080",
                "proxy",
                "listen=tcp:127.0.0.1:8080",
                "connect=tcp:127.0.0.1:80",
            ],
        ),
        (
            Change(
                "mounts",
                Effect.LIVE,
                "",
                "device-add",
                "nixant-mount-a",
                {"path": "/a"},
            ),
            [
                "config",
                "device",
                "add",
                "local:dev",
                "nixant-mount-a",
                "disk",
                "path=/a",
            ],
        ),
        (
            Change(
                "mounts",
                Effect.LIVE,
                "",
                "device-set",
                "nixant-mount-a",
                {"readonly": "true"},
            ),
            ["config", "device", "set", "local:dev", "nixant-mount-a", "readonly=true"],
        ),
        (
            Change("mounts", Effect.LIVE, "", "device-remove", "nixant-mount-a"),
            ["config", "device", "remove", "local:dev", "nixant-mount-a"],
        ),
        (
            Change(
                "disk",
                Effect.LIVE,
                "",
                "root-size",
                "root",
                {"size": "99", "local": ""},
            ),
            ["config", "device", "override", "local:dev", "root", "size=99"],
        ),
        (
            Change(
                "disk",
                Effect.LIVE,
                "",
                "root-size",
                "root",
                {"size": "99", "local": "true"},
            ),
            ["config", "device", "set", "local:dev", "root", "size=99"],
        ),
    ],
)
def test_apply_change(provider: IncusProvider, change: Change, argv: list[str]) -> None:
    provider.apply("dev", change)
    provider.runner.run.assert_called_once_with(["incus", *argv])


def test_apply_rejects_unknown_operation(provider: IncusProvider) -> None:
    with pytest.raises(NixantError, match="unknown change"):
        provider.apply("dev", Change("x", Effect.LIVE, "", "explode"))


@pytest.mark.parametrize(
    ("driver", "fails"), [("dir", True), ("btrfs", False), ("zfs", False)]
)
def test_check_quota_by_driver(
    provider: IncusProvider, driver: str, fails: bool
) -> None:
    provider.runner.run.return_value = response({"driver": driver})
    if fails:
        with pytest.raises(NixantError, match="driver dir"):
            provider.check_quota("default")
    else:
        provider.check_quota("default")
    provider.runner.run.assert_called_once_with(
        ["incus", "query", "local:/1.0/storage-pools/default"], capture=True
    )


def test_check_quota_reads_default_profile_pool(provider: IncusProvider) -> None:
    provider.runner.run.side_effect = [
        response({"devices": {"root": {"pool": "tank"}}}),
        response({"driver": "zfs"}),
    ]
    provider.check_quota(None)
    assert provider.runner.run.call_args_list[1].args[0][-1].endswith("/tank")


@pytest.mark.parametrize(
    ("incus_type", "kind"), [("container", "container"), ("virtual-machine", "vm")]
)
def test_inspect_normalizes_instance_kind(
    provider: IncusProvider, incus_type: str, kind: str
) -> None:
    provider.runner.run.return_value = response(
        {
            "name": "dev",
            "status": "Running",
            "type": incus_type,
            "config": {},
            "devices": {},
        }
    )
    state = provider.inspect("dev")
    assert state is not None and state.kind == kind


def test_create_ephemeral(provider: IncusProvider) -> None:
    data = json.loads(
        (Path(__file__).parents[2] / "nix/tests/runtime.json").read_text()
    )
    spec = replace(MachineSpec.from_runtime(data), mounts=(), ephemeral=True)
    provider.create(spec, {})
    assert "--ephemeral" in provider.runner.run.call_args.args[0]


def test_inspect_reads_ephemeral_flag(provider: IncusProvider) -> None:
    provider.runner.run.return_value = response(
        {
            "name": "dev",
            "status": "Running",
            "type": "container",
            "config": {},
            "devices": {},
            "ephemeral": True,
        }
    )
    state = provider.inspect("dev")
    assert state is not None and state.ephemeral is True

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from nixant.errors import CommandError, NixantError
from nixant.incus import IncusProvider
from nixant.models import MachineSpec, MountSpec
from nixant.planner import (
    Action,
    AddDevice,
    Change,
    Effect,
    RemoveDevice,
    SetConfig,
    SetDevice,
    SetRootSize,
    mount_device,
)
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


@pytest.mark.parametrize("kind", ["container", "vm"])
def test_create_overrides_root_size(provider: IncusProvider, kind: str) -> None:
    data = json.loads(
        (Path(__file__).parents[2] / "nix/tests/runtime.json").read_text()
    )
    spec = replace(
        MachineSpec.from_runtime(data), kind=kind, mounts=(), disk_bytes=10 * 2**30
    )
    provider.create(spec, {})
    argv = provider.runner.run.call_args.args[0]
    assert argv[-2:] == ["-d", f"root,size={10 * 2**30}"]


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


@pytest.mark.parametrize("user", [None, "dev"])
def test_run_without_input_never_reads_nixants_stdin(
    provider: IncusProvider, user: str | None
) -> None:
    provider.run("dev", ["true"], user=user)
    argv = provider.runner.run.call_args.args[0]
    assert argv[:4] == ["incus", "exec", "-T", "-n"]
    assert provider.runner.run.call_args.kwargs["stdin"] is None


MOUNTS = b"source /workspace none rw 0 0\n"


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr("nixant.incus.time.sleep", sleeps.append)
    return sleeps


def test_verify_mounts_never_touches_mounted_devices(
    provider: IncusProvider, tmp_path: Path, no_wait: list[float]
) -> None:
    provider.run = Mock(return_value=subprocess.CompletedProcess([], 0, MOUNTS))
    provider.verify_mounts("dev", [MountSpec("workspace", str(tmp_path), "/workspace")])
    provider.runner.run.assert_not_called()
    provider.run.assert_called_once_with("dev", ["cat", "/proc/mounts"], capture=True)


def test_verify_mounts_waits_for_a_late_mount(
    provider: IncusProvider, tmp_path: Path, no_wait: list[float]
) -> None:
    absent = subprocess.CompletedProcess([], 0, b"")
    provider.run = Mock(
        side_effect=[absent, absent, absent, subprocess.CompletedProcess([], 0, MOUNTS)]
    )
    provider.verify_mounts("dev", [MountSpec("workspace", str(tmp_path), "/workspace")])
    provider.runner.run.assert_not_called()  # waited instead of re-attaching
    assert no_wait == [0.5, 0.5, 0.5]


def test_verify_mounts_reattaches_through_planned_changes(
    provider: IncusProvider,
    tmp_path: Path,
    no_wait: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nixant.incus.MOUNT_POLLS", 1)
    provider.run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, b""),
            subprocess.CompletedProcess(
                [], 0, b"source /work\\040space virtiofs rw 0 0\n"
            ),
        ]
    )
    mount = MountSpec("workspace", str(tmp_path), "/work space")
    provider.verify_mounts("dev", [mount])
    calls = [c.args[0] for c in provider.runner.run.call_args_list]
    assert calls == [
        ["incus", "config", "device", "remove", "local:dev", "nixant-mount-workspace"],
        [
            "incus",
            "config",
            "device",
            "add",
            "local:dev",
            "nixant-mount-workspace",
            "disk",
            *[f"{k}={v}" for k, v in mount_device(mount).items()],
        ],
    ]


def test_verify_mounts_gives_up_after_two_reattachments(
    provider: IncusProvider,
    tmp_path: Path,
    no_wait: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nixant.incus.MOUNT_POLLS", 1)
    provider.run = Mock(return_value=subprocess.CompletedProcess([], 0, b""))
    with pytest.raises(NixantError, match="did not appear"):
        provider.verify_mounts(
            "dev", [MountSpec("workspace", str(tmp_path), "/workspace")]
        )
    assert provider.run.call_count == 3
    assert provider.runner.run.call_count == 4


def test_shift_failure_never_retries_unshifted(
    provider: IncusProvider,
    tmp_path: Path,
    no_wait: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nixant.incus.MOUNT_POLLS", 1)
    provider.run = Mock(return_value=subprocess.CompletedProcess([], 0, b""))
    provider.runner.run.side_effect = [
        subprocess.CompletedProcess([], 0, b""),
        CommandError(["incus"], 1, b"shift unsupported"),
    ]
    with pytest.raises(CommandError, match="shift unsupported"):
        provider.verify_mounts(
            "dev", [MountSpec("workspace", str(tmp_path), "/workspace")]
        )
    assert provider.runner.run.call_count == 2
    assert "shift=true" in provider.runner.run.call_args.args[0]


def test_create_attaches_no_devices(provider: IncusProvider, tmp_path: Path) -> None:
    spec = MachineSpec.from_runtime(
        json.loads((Path(__file__).parents[2] / "nix/tests/runtime.json").read_text())
    )
    provider.create(
        replace(spec, mounts=(MountSpec("workspace", str(tmp_path), "/workspace"),)),
        {},
    )
    provider.runner.run.assert_called_once()
    assert provider.runner.run.call_args.args[0][:2] == ["incus", "create"]


def test_metadata_scope(provider: IncusProvider) -> None:
    with pytest.raises(NixantError, match="non-nixant"):
        provider.set_metadata("dev", {"security.privileged": "true"})
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


def live(action: Action) -> Change:
    return Change("x", Effect.LIVE, "", action)


@pytest.mark.parametrize(
    ("change", "argv"),
    [
        (
            live(SetConfig("limits.cpu", "2")),
            ["config", "set", "local:dev", "limits.cpu=2"],
        ),
        (
            live(
                AddDevice(
                    "nixant-port-8080",
                    "proxy",
                    {"listen": "tcp:127.0.0.1:8080", "connect": "tcp:127.0.0.1:80"},
                )
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
            live(AddDevice("nixant-mount-a", "disk", {"path": "/a"})),
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
            live(SetDevice("nixant-mount-a", {"readonly": "true"})),
            ["config", "device", "set", "local:dev", "nixant-mount-a", "readonly=true"],
        ),
        (
            live(RemoveDevice("nixant-mount-a")),
            ["config", "device", "remove", "local:dev", "nixant-mount-a"],
        ),
        (
            live(SetRootSize(99, inherited=True)),
            ["config", "device", "override", "local:dev", "root", "size=99"],
        ),
        (
            live(SetRootSize(99, inherited=False)),
            ["config", "device", "set", "local:dev", "root", "size=99"],
        ),
    ],
)
def test_apply_change(provider: IncusProvider, change: Change, argv: list[str]) -> None:
    provider.apply("dev", change)
    provider.runner.run.assert_called_once_with(["incus", *argv])


def test_unsupported_change_cannot_be_applied(provider: IncusProvider) -> None:
    with pytest.raises(NixantError, match="cannot be applied"):
        provider.apply("dev", Change("x", Effect.UNSUPPORTED, "nope"))
    provider.runner.run.assert_not_called()


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

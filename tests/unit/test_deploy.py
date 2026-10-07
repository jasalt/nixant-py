import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nixant.cli import app, parse_duration
from nixant.errors import NixantError, UsageError
from nixant.models import MachineSpec, MachineState
from nixant.nix.activate import can_skip
from nixant.nix.eval import Evaluation
from nixant.ownership import PREFIX
from nixant.providers.incus import IncusProvider
from nixant.run import Runner


@pytest.fixture
def deploy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Mock]:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("nixant.cli.os.getuid", lambda: 1000)
    spec = MachineSpec.from_runtime(
        json.loads((Path(__file__).parents[2] / "nix/tests/runtime.json").read_text())
    )
    mocks = {}
    for name, value in {
        "check_host_tools": None,
        "discover_project": tmp_path,
        "evaluate": Evaluation(spec, "system.drv"),
        "build": "system",
        "resolve": None,
        "wait_ready": "running",
        "activate": "ok",
        "can_skip": False,
    }.items():
        mocks[name] = Mock(return_value=value)
        monkeypatch.setattr(f"nixant.cli.{name}", mocks[name])
    mocks["provider"] = Mock()
    mocks["provider"].inspect.return_value = MachineState(
        "test-dev", "Stopped", "container", {}, {}
    )
    monkeypatch.setattr(
        "nixant.cli.IncusProvider", Mock(return_value=mocks["provider"])
    )
    return mocks


def test_up_order(deploy: dict[str, Mock]) -> None:
    events = Mock()
    for name in ("evaluate", "build", "resolve", "provider", "wait_ready", "activate"):
        events.attach_mock(deploy[name], name)
    result = CliRunner().invoke(app, ["up", "--timeout", "5m"])
    assert result.exit_code == 0, result.output
    calls = [call[0] for call in events.mock_calls]
    assert calls.index("evaluate") < calls.index("build") < calls.index("resolve")
    assert (
        calls.index("provider.create")
        < calls.index("provider.start")
        < calls.index("wait_ready")
    )
    assert calls.index("provider.ensure_mount") < calls.index("activate")
    assert deploy["activate"].call_args.kwargs["timeout"] == 300


def test_uid_and_mount_before_incus(
    deploy: dict[str, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nixant.cli.os.getuid", lambda: 999)
    result = CliRunner().invoke(app, ["up"])
    assert result.exit_code == 1
    assert "nixant.user.uid = 999" in result.output
    deploy["resolve"].assert_not_called()
    deploy["build"].assert_not_called()
    monkeypatch.setattr("nixant.cli.os.getuid", lambda: 1000)
    monkeypatch.setattr(
        "nixant.cli.resolve_mount_sources",
        Mock(side_effect=NixantError("missing mount")),
    )
    assert CliRunner().invoke(app, ["up"]).exit_code == 1
    deploy["resolve"].assert_not_called()
    deploy["build"].assert_not_called()


@pytest.mark.parametrize("status", ["Stopped", "Frozen", "Running", "Error"])
def test_up_states(deploy: dict[str, Mock], status: str) -> None:
    deploy["resolve"].return_value = MachineState(
        "test-dev", status, "container", {}, {}
    )
    result = CliRunner().invoke(app, ["up"])
    assert result.exit_code == (1 if status == "Error" else 0)
    assert deploy["provider"].start.called == (status in ("Stopped", "Frozen"))
    deploy["provider"].create.assert_not_called()


def test_rebuild_always_activates_without_mount_changes(
    deploy: dict[str, Mock],
) -> None:
    deploy["resolve"].return_value = MachineState(
        "test-dev", "Running", "container", {}, {}
    )
    deploy["can_skip"].return_value = True
    result = CliRunner().invoke(app, ["rebuild"])
    assert result.exit_code == 0, result.output
    assert "outer settings differ (mounts)" in result.output
    deploy["activate"].assert_called_once()
    deploy["can_skip"].assert_not_called()
    deploy["provider"].create.assert_not_called()
    deploy["provider"].ensure_mount.assert_not_called()


def test_rebuild_missing(deploy: dict[str, Mock]) -> None:
    result = CliRunner().invoke(app, ["rebuild"])
    assert result.exit_code == 1
    assert "run nixant up" in result.output
    deploy["provider"].create.assert_not_called()


def test_up_skip(deploy: dict[str, Mock]) -> None:
    deploy["resolve"].return_value = MachineState(
        "test-dev", "Running", "container", {}, {}
    )
    deploy["can_skip"].return_value = True
    assert CliRunner().invoke(app, ["up"]).exit_code == 0
    deploy["activate"].assert_not_called()


def test_eval_failure_before_incus(deploy: dict[str, Mock]) -> None:
    deploy["evaluate"].side_effect = NixantError("syntax error")
    assert CliRunner().invoke(app, ["up"]).exit_code == 1
    deploy["resolve"].assert_not_called()
    deploy["provider"].create.assert_not_called()


def test_limits_applied_to_new_instance_before_start(
    deploy: dict[str, Mock],
) -> None:
    original = deploy["evaluate"].return_value
    deploy["evaluate"].return_value = replace(
        original, spec=replace(original.spec, cpus=2, memory_bytes=2**30)
    )
    events = Mock()
    events.attach_mock(deploy["provider"], "provider")
    result = CliRunner().invoke(app, ["up"])
    assert result.exit_code == 0, result.output
    keys = [
        call.args[1].key
        for call in deploy["provider"].apply.call_args_list
        if call.args[1].op == "config"
    ]
    assert keys == ["limits.cpu", "limits.memory"]
    names = [call[0] for call in events.mock_calls]
    assert names.index("provider.create") < names.index("provider.apply")
    assert names.index("provider.apply") < names.index("provider.start")


def test_disk_checks_quota_before_creating(deploy: dict[str, Mock]) -> None:
    original = deploy["evaluate"].return_value
    deploy["evaluate"].return_value = replace(
        original, spec=replace(original.spec, disk_bytes=2**30)
    )
    deploy["provider"].check_quota.side_effect = NixantError("driver dir")
    result = CliRunner().invoke(app, ["up"])
    assert result.exit_code == 1
    deploy["provider"].create.assert_not_called()


def test_shrink_is_refused_on_existing_instance(deploy: dict[str, Mock]) -> None:
    original = deploy["evaluate"].return_value
    deploy["evaluate"].return_value = replace(
        original, spec=replace(original.spec, disk_bytes=2**30)
    )
    deploy["resolve"].return_value = MachineState(
        "test-dev",
        "Running",
        "container",
        {},
        {},
        expanded_devices={"root": {"type": "disk", "pool": "p", "size": "10GiB"}},
    )
    result = CliRunner().invoke(app, ["up"])
    assert result.exit_code == 1
    assert "cannot shrink" in result.output
    deploy["provider"].apply.assert_not_called()


@pytest.mark.parametrize(
    ("value", "expected"), [(None, None), ("5m", 300), ("1.5h", 5400), ("30", 30)]
)
def test_duration(value: str | None, expected: float | None) -> None:
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["0", "-1", "nan", "infinity", "five", "1d"])
def test_invalid_duration(value: str) -> None:
    with pytest.raises(UsageError):
        parse_duration(value)


@pytest.fixture
def running_deploy(
    deploy: dict[str, Mock], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict:
    """Exercise the real provider/skip check against an in-memory Incus response."""
    data = {
        "name": "test-dev",
        "status": "Running",
        "type": "container",
        "config": {
            PREFIX + "activation": "ok",
            PREFIX + "system": "system",
            PREFIX + "user": "dev",
            PREFIX + "workdir": "/workspace",
        },
        "devices": {
            "nixant-mount-workspace": {
                "type": "disk",
                "source": str(tmp_path),
                "path": "/workspace",
                "shift": "true",
                "readonly": "false",
            },
            "unrelated": {"type": "disk", "source": "/other", "path": "/other"},
        },
    }
    runner = Mock(spec=Runner)

    def run(argv, **kwargs):
        stdout = b""
        if argv[:2] == ["incus", "query"]:
            stdout = json.dumps(data).encode()
        elif argv[:3] == ["incus", "config", "set"]:
            data["config"].update(item.split("=", 1) for item in argv[4:])
        elif argv[:4] == ["incus", "config", "device", "remove"]:
            del data["devices"][argv[5]]
        elif argv[:4] == ["incus", "config", "device", "add"]:
            data["devices"][argv[5]] = {
                "type": argv[6],
                **dict(item.split("=", 1) for item in argv[7:]),
            }
        elif argv[:3] == ["incus", "exec", "-T"]:
            command = argv[argv.index("--") + 1 :]
            if command == ["readlink", "-f", "/run/current-system"]:
                stdout = b"system\n"
            elif command == ["cat", "/proc/mounts"]:
                stdout = b"source /workspace none rw 0 0\n"
            else:
                raise RuntimeError(f"unexpected guest command: {argv}")
        else:
            raise RuntimeError(f"unexpected Incus command: {argv}")
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    runner.run.side_effect = run
    provider = IncusProvider(runner)
    deploy["resolve"].return_value = provider.inspect("test-dev")
    monkeypatch.setattr("nixant.cli.IncusProvider", lambda runner: provider)
    monkeypatch.setattr("nixant.cli.can_skip", can_skip)
    return data


def test_up_refreshes_workdir_without_switching(
    deploy: dict[str, Mock], running_deploy: dict
) -> None:
    original = deploy["evaluate"].return_value
    deploy["evaluate"].return_value = replace(
        original, spec=replace(original.spec, workdir="/tmp")
    )
    result = CliRunner().invoke(app, ["up"])
    if result.exception is not None:
        raise result.exception
    assert result.exit_code == 0, result.output
    assert running_deploy["config"][PREFIX + "workdir"] == "/tmp"
    deploy["activate"].assert_not_called()


def test_up_removes_obsolete_mount_without_touching_unrelated_devices(
    deploy: dict[str, Mock], running_deploy: dict
) -> None:
    original = deploy["evaluate"].return_value
    deploy["evaluate"].return_value = replace(
        original, spec=replace(original.spec, mounts=())
    )
    result = CliRunner().invoke(app, ["up"])
    if result.exception is not None:
        raise result.exception
    assert result.exit_code == 0, result.output
    assert "nixant-mount-workspace" not in running_deploy["devices"]
    assert running_deploy["devices"]["unrelated"] == {
        "type": "disk",
        "source": "/other",
        "path": "/other",
    }


def test_up_replaces_renamed_mount(
    deploy: dict[str, Mock], running_deploy: dict
) -> None:
    original = deploy["evaluate"].return_value
    renamed = replace(original.spec.mounts[0], name="code")
    deploy["evaluate"].return_value = replace(
        original, spec=replace(original.spec, mounts=(renamed,))
    )
    result = CliRunner().invoke(app, ["up"])
    assert result.exit_code == 0, result.output
    assert "nixant-mount-workspace" not in running_deploy["devices"]
    assert "unrelated" in running_deploy["devices"]


def test_up_uses_git_override_name(
    deploy: dict[str, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nixant.cli.get_override", Mock(return_value="shop-mine"))
    result = CliRunner().invoke(app, ["up"])
    assert result.exit_code == 0, result.output
    assert deploy["resolve"].call_args.args[3] == "shop-mine"
    assert deploy["resolve"].call_args.kwargs["name_source"] == "git override"
    assert deploy["provider"].create.call_args.args[0].instance_name == "shop-mine"
    assert deploy["provider"].start.call_args.args == ("shop-mine",)


def test_up_without_override_uses_config_name(deploy: dict[str, Mock]) -> None:
    assert CliRunner().invoke(app, ["up"]).exit_code == 0
    assert deploy["resolve"].call_args.args[3] == "test-dev"
    assert deploy["resolve"].call_args.kwargs["name_source"] == "config"


def test_restart_effect_changes_are_stored_and_announced(
    deploy: dict[str, Mock],
) -> None:
    original = deploy["evaluate"].return_value
    deploy["evaluate"].return_value = replace(
        original, spec=replace(original.spec, kind="vm", disk_bytes=2**34)
    )
    deploy["resolve"].return_value = MachineState(
        "test-dev",
        "Running",
        "vm",
        {},
        {"nixant-mount-workspace": {"type": "disk"}},
        expanded_devices={"root": {"type": "disk", "pool": "p", "size": "10GiB"}},
    )
    deploy["can_skip"].return_value = True
    result = CliRunner().invoke(app, ["up"])
    assert result.exit_code == 0, result.output
    ops = [call.args[1].op for call in deploy["provider"].apply.call_args_list]
    assert "root-size" in ops
    assert "takes effect after the next restart" in result.output

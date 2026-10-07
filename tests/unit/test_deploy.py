import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nixant.cli import app, parse_duration
from nixant.errors import NixantError, UsageError
from nixant.models import MachineSpec, MachineState
from nixant.nix.eval import Evaluation


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
    mocks["provider"].inspect.return_value = None
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
    assert "outer mount settings differ" in result.output
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


def test_limits_not_silently_ignored(deploy: dict[str, Mock]) -> None:
    original = deploy["evaluate"].return_value
    deploy["evaluate"].return_value = replace(
        original, spec=replace(original.spec, cpus=2)
    )
    result = CliRunner().invoke(app, ["up"])
    assert result.exit_code == 1
    assert "Phase 2" in result.output
    deploy["build"].assert_not_called()


@pytest.mark.parametrize(
    ("value", "expected"), [(None, None), ("5m", 300), ("1.5h", 5400), ("30", 30)]
)
def test_duration(value: str | None, expected: float | None) -> None:
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["0", "-1", "nan", "infinity", "five", "1d"])
def test_invalid_duration(value: str) -> None:
    with pytest.raises(UsageError):
        parse_duration(value)

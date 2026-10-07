import json
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock

import pytest

from nixant.errors import NixantError, UsageError
from nixant.models import MachineSpec
from nixant.nix.build import build, gcroot_path
from nixant.nix.eval import evaluate
from nixant.run import Runner

DRV = "/nix/store/" + "a" * 32 + "-system.drv"
SYSTEM = "/nix/store/" + "b" * 32 + "-system"


@pytest.fixture
def runtime() -> dict:
    return json.loads(
        (Path(__file__).parents[2] / "nix/tests/runtime.json").read_text()
    )


def runner_with(data: object) -> Mock:
    runner = Mock(spec=Runner)
    runner.run.side_effect = [
        subprocess.CompletedProcess([], 0, b"true\n"),
        subprocess.CompletedProcess([], 0, b""),
        subprocess.CompletedProcess([], 0, json.dumps(data).encode()),
    ]
    return runner


def test_frozen_runtime(runtime: dict) -> None:
    spec = MachineSpec.from_runtime(runtime)
    assert spec.user.uid == 1000
    assert spec.mounts[0].read_only is False
    with pytest.raises(FrozenInstanceError):
        spec.kind = "vm"


@pytest.mark.parametrize("version", [None, 2, True, "1"])
def test_schema_drift(runtime: dict, version: object) -> None:
    runtime["schemaVersion"] = version
    with pytest.raises(NixantError, match="matching nixant version"):
        MachineSpec.from_runtime(runtime)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("kind", "unknown"),
        ("instanceName", "-option"),
        ("cpus", True),
        ("memoryBytes", -1),
        ("workdir", "relative"),
        ("mounts", []),
        ("ports", {}),
    ],
)
def test_invalid_runtime(runtime: dict, key: str, value: object) -> None:
    runtime[key] = value
    with pytest.raises(NixantError, match="invalid nixant runtime"):
        MachineSpec.from_runtime(runtime)


def test_single_evaluation(
    runtime: dict, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = runner_with({"runtime": runtime, "drvPath": DRV})
    result = evaluate(tmp_path, "dev", runner)
    assert result.drv_path == DRV
    calls = [call for call in runner.run.call_args_list if call.args[0][0] == "nix"]
    assert len(calls) == 1
    assert "drvPath = c.system.build.toplevel.drvPath" in calls[0].args[0][-1]
    assert calls[0].kwargs == {"cwd": tmp_path, "capture": True}
    assert "evaluating dev" in capsys.readouterr().err


def test_runtime_only(runtime: dict, tmp_path: Path) -> None:
    runner = runner_with({"runtime": runtime})
    assert evaluate(tmp_path, "dev", runner, with_derivation=False).drv_path is None
    assert "toplevel" not in runner.run.call_args.args[0][-1]


def test_missing_target(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="available targets: other"):
        evaluate(
            tmp_path, "dev", runner_with({"error": "target", "targets": ["other"]})
        )


def test_missing_module(tmp_path: Path) -> None:
    with pytest.raises(NixantError, match="nixant.nixosModules.container"):
        evaluate(tmp_path, "dev", runner_with({"error": "module"}))


def test_invalid_target_no_commands(tmp_path: Path) -> None:
    runner = Mock(spec=Runner)
    with pytest.raises(UsageError):
        evaluate(tmp_path, "../bad", runner)
    runner.run.assert_not_called()


def test_build_by_derivation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    runner = Mock(spec=Runner)
    runner.run.return_value = subprocess.CompletedProcess(
        [], 0, json.dumps([{"outputs": {"out": SYSTEM}}]).encode()
    )
    assert build(tmp_path, "dev", DRV, runner) == SYSTEM
    link = gcroot_path(tmp_path, "dev")
    assert link.parent.is_dir()
    runner.run.assert_called_once_with(
        ["nix", "build", DRV + "^out", "--out-link", str(link), "--json"],
        cwd=tmp_path,
        capture=True,
    )


def test_invalid_drv_no_build(tmp_path: Path) -> None:
    runner = Mock(spec=Runner)
    with pytest.raises(NixantError, match="invalid system derivation"):
        build(tmp_path, "dev", "--option", runner)
    runner.run.assert_not_called()


@pytest.mark.parametrize("response", [[], [{}], [{"outputs": {"out": "/tmp/unsafe"}}]])
def test_bad_build_response(
    response: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    runner = Mock(spec=Runner)
    runner.run.return_value = subprocess.CompletedProcess(
        [], 0, json.dumps(response).encode()
    )
    with pytest.raises(NixantError, match="invalid Nix build response"):
        build(tmp_path, "dev", DRV, runner)

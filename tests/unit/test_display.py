import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nixant.cli import app
from nixant.models import MachineSpec, MachineState
from nixant.nix.build import gcroot_path
from nixant.nix.eval import Evaluation
from nixant.ownership import PREFIX, metadata


@pytest.fixture
def display(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Mock, Mock, Path]:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("nixant.cli.check_host_tools", lambda: None)
    monkeypatch.setattr("nixant.cli.discover_project", lambda: tmp_path)
    provider = Mock()
    provider.find.return_value = []
    monkeypatch.setattr("nixant.cli.IncusProvider", lambda _: provider)
    evaluate = Mock()
    monkeypatch.setattr("nixant.cli.evaluate", evaluate)
    return provider, evaluate, tmp_path


def test_status_no_evaluation(display: tuple) -> None:
    provider, evaluate, root = display
    config = {
        **metadata(root, "dev"),
        PREFIX + "schema": "2",
        PREFIX + "system": "/nix/store/system",
        PREFIX + "activation": "degraded",
    }
    provider.find.return_value = [
        MachineState("test-dev", "Running", "container", config, {}, ("10.0.0.2",))
    ]
    link = gcroot_path(root, "dev")
    link.parent.mkdir(parents=True)
    link.symlink_to("/nix/store/system")
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "RUNNING  container  10.0.0.2" in result.output
    assert "degraded, current; schema mismatch" in result.output
    assert str(root) in result.output
    evaluate.assert_not_called()


def test_status_absent(display: tuple) -> None:
    result = CliRunner().invoke(app, ["status", "dev"])
    assert result.exit_code == 0
    assert "dev: not created" in result.output
    display[1].assert_not_called()


def test_config_roundtrip(display: tuple) -> None:
    provider, evaluate, root = display
    runtime = json.loads(
        (Path(__file__).parents[2] / "nix/tests/runtime.json").read_text()
    )
    spec = MachineSpec.from_runtime(runtime)
    assert spec.to_runtime() == runtime
    evaluate.return_value = Evaluation(spec, None)
    result = CliRunner().invoke(app, ["config"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["runtime"] == runtime
    assert data["mountSources"]["workspace"] == str(root)
    assert data["instanceNameSource"] == "config"
    assert evaluate.call_args.kwargs["with_derivation"] is False
    provider.find.assert_not_called()

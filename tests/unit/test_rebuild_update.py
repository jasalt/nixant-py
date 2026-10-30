from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nixant.cli import app
from nixant.errors import NixantError


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runner = Mock()
    rebuild = Mock()
    monkeypatch.setattr("nixant.cli.check_host_tools", lambda: None)
    monkeypatch.setattr("nixant.cli.Runner", lambda **kwargs: runner)
    monkeypatch.setattr("nixant.cli.discover_project", lambda: tmp_path)
    monkeypatch.setattr("nixant.cli.target_lock", lambda *args: nullcontext())
    monkeypatch.setattr("nixant.cli.deploy.rebuild", rebuild)
    return tmp_path, runner, rebuild


@pytest.mark.parametrize("command", ["rebuild", "reload"])
@pytest.mark.parametrize(
    "options,inputs",
    [([], None), (["--update"], []),
     (["--update-input", "devtools", "--update-input", "pig-extensions",
       "--update-input", "devtools"], ["devtools", "pig-extensions"])],
)
def test_refresh_before_rebuild(setup, command, options, inputs):
    root, runner, rebuild = setup
    events = []
    runner.run.side_effect = lambda *a, **kw: events.append("update")
    rebuild.side_effect = lambda *a: events.append("rebuild")
    result = CliRunner().invoke(app, [command, "dev", *options])
    assert result.exit_code == 0, result.output
    if inputs is None:
        runner.run.assert_not_called()
        assert events == ["rebuild"]
    else:
        runner.run.assert_called_once_with(
            ["nix", "flake", "update", *inputs], cwd=root / ".nixant"
        )
        assert events == ["update", "rebuild"]
    assert rebuild.call_args.args[2:] == (root, "dev", 1800.0)


def test_update_failure_does_not_rebuild(setup):
    _, runner, rebuild = setup
    runner.run.side_effect = NixantError("update failed")
    result = CliRunner().invoke(app, ["rebuild", "dev", "--update"])
    assert result.exit_code == 1
    assert "update failed" in result.output
    rebuild.assert_not_called()


@pytest.mark.parametrize("options", [
    ["--update", "--update-input", "devtools"],
    ["--update-input=--impure"],
    ["--update-input="],
    ["--update", "--timeout", "invalid"],
])
def test_invalid_options_do_not_update(setup, options):
    _, runner, rebuild = setup
    result = CliRunner().invoke(app, ["rebuild", "dev", *options])
    assert result.exit_code == 2
    runner.run.assert_not_called()
    rebuild.assert_not_called()

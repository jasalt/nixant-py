from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nixant.cli import app
from nixant.models import MachineState
from nixant.ownership import PREFIX
from nixant.providers.incus import IncusProvider
from nixant.run import Runner


@pytest.fixture
def enter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Mock, Mock]:
    monkeypatch.setattr("nixant.cli.check_host_tools", lambda: None)
    monkeypatch.setattr("nixant.cli.discover_project", lambda: tmp_path)
    lookup = Mock(
        return_value=MachineState(
            "owned-dev",
            "Running",
            "container",
            {
                PREFIX + "user": "dev",
                PREFIX + "workdir": "/workspace",
            },
            {},
        )
    )
    monkeypatch.setattr("nixant.cli.lookup", lookup)
    monkeypatch.setattr(
        "nixant.cli.IncusProvider", lambda runner: IncusProvider(Runner())
    )
    execute = Mock()
    monkeypatch.setattr("nixant.cli.os.execvp", execute)
    for evaluation in ("nixant.deploy.evaluate", "nixant.cli.evaluate_spec"):
        monkeypatch.setattr(
            evaluation, Mock(side_effect=AssertionError("must not evaluate"))
        )
    return lookup, execute


@pytest.mark.parametrize(
    ("arguments", "target", "command"),
    [
        (["exec", "--", "hostname"], "dev", ["hostname"]),
        (["exec", "ls", "-la"], "dev", ["ls", "-la"]),
        (
            ["exec", "-n", "test", "--", "printf", "%s", "a; b"],
            "test",
            ["printf", "%s", "a; b"],
        ),
        (["exec", "echo", "-n", "test"], "dev", ["echo", "-n", "test"]),
    ],
)
def test_exec_arguments(
    enter: tuple[Mock, Mock], arguments: list[str], target: str, command: list[str]
) -> None:
    lookup, execute = enter
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert lookup.call_args.args[2] == target
    args = execute.call_args.args[1]
    assert args[-len(command) :] == command
    assert 'exec "$@"' in args
    assert "--cwd" in args
    assert "runuser" in " ".join(args)


def test_shell(enter: tuple[Mock, Mock]) -> None:
    _, execute = enter
    assert CliRunner().invoke(app, ["shell"]).exit_code == 0
    assert (
        'exec -l "$(getent passwd "$(id -un)" | cut -d: -f7)"'
        in execute.call_args.args[1]
    )


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (None, "does not exist"),
        (MachineState("dev", "Stopped", "container", {}, {}), "not running"),
        (
            MachineState("dev", "Running", "container", {}, {}),
            "no completed activation",
        ),
    ],
)
def test_enter_preconditions(
    enter: tuple[Mock, Mock], state: MachineState | None, message: str
) -> None:
    lookup, execute = enter
    lookup.return_value = state
    result = CliRunner().invoke(app, ["exec", "true"])
    assert result.exit_code == 1
    assert message in result.output
    execute.assert_not_called()


def test_exec_missing_command(enter: tuple[Mock, Mock]) -> None:
    assert CliRunner().invoke(app, ["exec"]).exit_code == 2
    enter[1].assert_not_called()

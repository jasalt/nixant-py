from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nixant.cli import app
from nixant.incus import IncusProvider
from nixant.models import MachineState
from nixant.ownership import PREFIX, metadata
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


@pytest.fixture
def mismatched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Mock:
    """The real lookup over an owned instance recorded with another schema."""
    monkeypatch.setattr("nixant.cli.check_host_tools", lambda: None)
    monkeypatch.setattr("nixant.cli.discover_project", lambda: tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = {
        **metadata(tmp_path, "dev"),
        PREFIX + "schema": "2",
        PREFIX + "user": "dev",
        PREFIX + "workdir": "/workspace",
    }
    provider = Mock(spec=IncusProvider)
    provider.find.return_value = [
        MachineState("shop-dev", "Running", "container", config, {})
    ]
    provider.snapshot_list.return_value = []
    monkeypatch.setattr("nixant.cli.IncusProvider", lambda runner: provider)
    monkeypatch.setattr("nixant.cli.os.execvp", Mock())
    monkeypatch.setattr("nixant.cli.wait_ready", Mock(return_value="running"))
    return provider


@pytest.mark.parametrize("arguments", [["shell"], ["exec", "true"]])
def test_schema_mismatch_blocks_commands_that_use_the_guest(
    mismatched: Mock, arguments: list[str]
) -> None:
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 1, result.output
    assert "uses nixant schema 2" in result.output


@pytest.mark.parametrize(
    "arguments",
    [["status"], ["down"], ["destroy"], ["snapshots"], ["snapshot", "s1"], ["restart"]],
)
def test_schema_mismatch_never_blocks_inspection_or_cleanup(
    mismatched: Mock, arguments: list[str]
) -> None:
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 0, result.output


def test_guest_entry_keeps_the_callers_stdin_and_terminal(
    enter: tuple[Mock, Mock],
) -> None:
    """shell/exec replace the process, so stdin and any terminal pass through.

    Without -T, incus exec allocates a pseudo-terminal only when stdin is a
    terminal and otherwise streams stdin to the command.
    """
    _, execute = enter
    assert CliRunner().invoke(app, ["exec", "cat"]).exit_code == 0
    argv = execute.call_args.args[1]
    assert argv[:3] == ["incus", "exec", "local:owned-dev"]
    assert not {"-T", "-t", "--force-noninteractive", "-n"} & set(
        argv[: argv.index("--")]
    )

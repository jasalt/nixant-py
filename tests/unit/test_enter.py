from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nixant.cli import app, logs_script
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
    monkeypatch.setattr(IncusProvider, "find", lambda self, metadata: [])
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


@pytest.mark.parametrize("command", ["shell", "ssh"])
def test_shell(enter: tuple[Mock, Mock], command: str) -> None:
    _, execute = enter
    assert CliRunner().invoke(app, [command]).exit_code == 0
    assert (
        'exec -l "$(getent passwd "$(id -un)" | cut -d: -f7)"'
        in execute.call_args.args[1]
    )


@pytest.mark.parametrize(
    ("arguments", "inferred", "target"),
    [
        (["shell"], "site", "site"),
        (["shell"], None, "dev"),
        (["shell", "other"], "site", "other"),
        (["exec", "true"], "site", "site"),
        (["exec", "-n", "other", "true"], "site", "other"),
    ],
)
def test_target_from_cwd(
    enter: tuple[Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    inferred: str | None,
    target: str,
) -> None:
    lookup, _ = enter
    target_at = Mock(return_value=inferred)
    monkeypatch.setattr("nixant.cli.target_at", target_at)
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert lookup.call_args.args[2] == target
    if target != "other":
        assert target_at.call_args.args[2] == Path.cwd()
    else:
        target_at.assert_not_called()


def test_enter_starts_in_the_matching_guest_directory(
    enter: tuple[Mock, Mock], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lookup, execute = enter
    site = tmp_path / "www/site"
    (site / "public_html").mkdir(parents=True)
    lookup.return_value = replace(
        lookup.return_value,
        devices={"nixant-mount-workspace": {"source": str(site), "path": "/workspace"}},
    )
    monkeypatch.chdir(site / "public_html")
    assert CliRunner().invoke(app, ["shell"]).exit_code == 0
    args = execute.call_args.args[1]
    assert args[args.index("--cwd") + 1] == "/workspace/public_html"
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(app, ["exec", "true"]).exit_code == 0
    args = execute.call_args.args[1]
    assert args[args.index("--cwd") + 1] == "/workspace"


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


@pytest.mark.parametrize("status", ["Stopped", "Frozen"])
def test_enter_offers_to_start_a_stopped_instance(
    enter: tuple[Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
) -> None:
    lookup, execute = enter
    lookup.return_value = replace(lookup.return_value, status=status)
    monkeypatch.setattr("nixant.cli._interactive", lambda: True)
    start = Mock()
    monkeypatch.setattr(IncusProvider, "start", start)
    ready = Mock(return_value="running")
    monkeypatch.setattr("nixant.cli.wait_ready", ready)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    started = MachineState(
        "owned-dev",
        "Running",
        "container",
        {PREFIX + "routes": '{"site.localhost": 8105}'},
        {
            "nixant-port-8105": {
                "type": "proxy",
                "listen": "tcp:127.0.0.1:8105",
                "connect": "tcp:127.0.0.1:80",
            }
        },
        ipv4=("10.0.0.5",),
    )
    monkeypatch.setattr(IncusProvider, "inspect", lambda self, name: started)
    result = CliRunner().invoke(app, ["shell"], input="\n")
    assert result.exit_code == 0, result.output
    assert "Do you want to start the instance owned-dev now? [Y/n]" in result.output
    assert "Starting the system container instance owned-dev\n" in result.output
    assert "owned-dev ready (dev) 10.0.0.5; nixant shell dev\n" in result.output
    assert "port 127.0.0.1:8105 -> guest 80\n" in result.output
    assert "route http://site.localhost -> http://127.0.0.1:8105\n" in result.output
    assert "served by `nixant proxy` on the host" in result.output
    start.assert_called_once_with("owned-dev")
    assert ready.call_args.args[1:] == ("owned-dev", "container")
    execute.assert_called_once()


def test_enter_declined_start_leaves_the_instance_stopped(
    enter: tuple[Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup, execute = enter
    lookup.return_value = replace(lookup.return_value, status="Stopped")
    monkeypatch.setattr("nixant.cli._interactive", lambda: True)
    start = Mock()
    monkeypatch.setattr(IncusProvider, "start", start)
    result = CliRunner().invoke(app, ["shell"], input="n\n")
    assert result.exit_code == 1
    assert "not running; run nixant restart dev" in result.output
    start.assert_not_called()
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
    [
        ["status"],
        ["down"],
        ["destroy", "--yes"],
        ["snapshots"],
        ["snapshot", "s1"],
        ["restart"],
    ],
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


def test_logs_default_is_the_journal(enter: tuple[Mock, Mock]) -> None:
    lookup, execute = enter
    result = CliRunner().invoke(app, ["logs"])
    assert result.exit_code == 0, result.output
    argv = execute.call_args.args[1]
    assert argv[:4] == ["incus", "exec", "local:owned-dev", "--"]
    assert argv[4:] == [
        "/run/current-system/sw/bin/sh",
        "-c",
        "journalctl --no-pager -n 50",
    ]


def test_logs_names_recorded_files_and_units(enter: tuple[Mock, Mock]) -> None:
    lookup, execute = enter
    state = lookup.return_value
    lookup.return_value = replace(
        state,
        config={**state.config, PREFIX + "logs": '{"debug": "/workspace/log/d.log"}'},
    )
    result = CliRunner().invoke(
        app, ["logs", "debug", "nginx", "-f", "-n", "5", "-t", "x"]
    )
    assert result.exit_code == 0, result.output
    assert lookup.call_args.args[2] == "x"
    assert execute.call_args.args[1][-1] == (
        "trap 'kill 0' EXIT INT TERM HUP; "
        "journalctl --no-pager -n 5 -f -u nginx & "
        "tail -n 5 -F -- /workspace/log/d.log & wait"
    )


def test_logs_script_files_only() -> None:
    recorded = {"debug": "/w/debug.log", "php": "/w/php error.log"}
    assert logs_script(recorded, ["debug", "php"], 10, False) == (
        "tail -n 10 -- /w/debug.log '/w/php error.log'"
    )
    assert logs_script(recorded, ["debug"], 10, True) == "tail -n 10 -F -- /w/debug.log"
    assert logs_script(recorded, ["debug", "sshd"], 3, False) == (
        "journalctl --no-pager -n 3 -u sshd; tail -n 3 -- /w/debug.log"
    )

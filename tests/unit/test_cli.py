import pytest
import typer
from typer.testing import CliRunner

from nixant.cli import app


def app_commands() -> list[str]:
    group = typer.main.get_command(app)
    assert isinstance(group, typer.core.TyperGroup)
    return sorted(group.commands)


def test_help() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "NixOS development environments" in result.stdout
    assert "--verbose" in result.stdout


def test_unknown_command_is_usage_error() -> None:
    result = CliRunner().invoke(app, ["not-a-command"])
    assert result.exit_code == 2


def test_version_includes_build_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIXANT_REV", "0123456789abcdef")
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "nixant 0.1.0 (0123456789ab)"


def test_version_without_revision() -> None:
    result = CliRunner().invoke(app, ["version"], env={"NIXANT_REV": ""})
    assert result.exit_code == 0
    assert result.stdout.strip() == "nixant 0.1.0 (revision unavailable)"


@pytest.fixture
def no_host_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nixant.run.shutil.which", lambda tool: None)


@pytest.mark.parametrize("command", app_commands())
def test_subcommand_help_needs_no_host_tools(no_host_tools: None, command: str) -> None:
    result = CliRunner().invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


@pytest.mark.parametrize(
    "arguments",
    [["status"], ["exec", "--", "ls", "--help"], ["exec", "ls", "--help"]],
)
def test_commands_still_check_host_tools(
    no_host_tools: None, arguments: list[str]
) -> None:
    """A --help after exec's command belongs to the guest command."""
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 1
    assert "required tools not found on PATH: incus, nix" in result.output

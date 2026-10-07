from typer.testing import CliRunner

from nixant.cli import app


def test_help() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "NixOS development environments" in result.stdout
    assert "--verbose" in result.stdout


def test_unknown_command_is_usage_error() -> None:
    result = CliRunner().invoke(app, ["not-a-command"])
    assert result.exit_code == 2

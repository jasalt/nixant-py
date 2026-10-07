import subprocess

import pytest
import typer
from typer.testing import CliRunner

from nixant.cli import ErrorHandlingGroup
from nixant.errors import NixantError, UsageError
from nixant.run import check_host_tools


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (NixantError("expected failure"), 1, "expected failure"),
        (UsageError("bad target"), 2, "bad target"),
        (KeyboardInterrupt(), 1, "interrupted"),
        (subprocess.TimeoutExpired(["nix"], 3), 1, "within 3s"),
    ],
)
def test_error_mapping(error: BaseException, code: int, message: str) -> None:
    app = typer.Typer(cls=ErrorHandlingGroup)

    @app.callback()
    def main() -> None:
        pass

    @app.command()
    def fail() -> None:
        raise error

    result = CliRunner().invoke(app, ["fail"])
    assert result.exit_code == code
    assert message in result.output
    assert "Traceback" not in result.output


def test_startup_check_and_help(monkeypatch: pytest.MonkeyPatch) -> None:
    from nixant.cli import app

    monkeypatch.setenv("PATH", "")
    assert CliRunner().invoke(app, ["--help"]).exit_code == 0
    # Use an isolated group so the scaffold need not expose a dummy command.
    probe = typer.Typer(cls=ErrorHandlingGroup)

    @probe.callback()
    def main() -> None:
        check_host_tools()

    @probe.command()
    def noop() -> None:
        pass

    result = CliRunner().invoke(probe, ["noop"])
    assert result.exit_code == 1
    assert "incus, nix, git" in result.output

"""Command-line entry point."""

import subprocess
from typing import Any

import click
import typer
from typer.core import TyperGroup

from nixant.errors import NixantError, UsageError
from nixant.run import Runner, check_host_tools


class ErrorHandlingGroup(TyperGroup):
    """Translate expected failures at the command boundary only."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except UsageError as exc:
            raise click.UsageError(str(exc), ctx) from exc
        except NixantError as exc:
            raise click.ClickException(str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise click.ClickException(
                f"command did not finish within {exc.timeout}s"
            ) from exc
        except KeyboardInterrupt as exc:
            raise click.ClickException("interrupted") from exc


app = typer.Typer(
    cls=ErrorHandlingGroup,
    no_args_is_help=True,
    help="NixOS development environments on local Incus.",
)


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Print commands before running."
    ),
) -> None:
    """Manage local NixOS development environments."""
    check_host_tools()
    ctx.ensure_object(dict)
    ctx.obj["runner"] = Runner(verbose=verbose)

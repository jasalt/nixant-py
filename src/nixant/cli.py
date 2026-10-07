"""Command-line entry point."""

import typer

app = typer.Typer(
    no_args_is_help=True, help="NixOS development environments on local Incus."
)


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Print commands before running."
    ),
) -> None:
    """Manage local NixOS development environments."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose

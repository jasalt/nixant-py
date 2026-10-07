"""Command-line entry point."""

import os
import re
import subprocess
from dataclasses import replace
from typing import Any

import click
import typer
from typer.core import TyperGroup

from nixant.errors import NixantError, UsageError
from nixant.nix.activate import activate, can_skip
from nixant.nix.build import build
from nixant.nix.eval import evaluate
from nixant.ownership import metadata, resolve
from nixant.project import discover_project, resolve_mount_sources, target_lock
from nixant.providers.incus import IncusProvider
from nixant.readiness import wait_ready
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


def parse_duration(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(s|m|h)?", value)
    if match is None:
        raise UsageError("timeout must be a positive duration, e.g. 90s, 5m or 1h")
    seconds = float(match[1]) * {None: 1, "s": 1, "m": 60, "h": 3600}[match[2]]
    if seconds <= 0 or seconds == float("inf"):
        raise UsageError("timeout must be a finite positive duration")
    return seconds


def _deploy(
    ctx: typer.Context, target: str, timeout: str | None, *, rebuild: bool
) -> None:
    duration = parse_duration(timeout)
    runner = ctx.obj["runner"]
    provider = IncusProvider(runner)
    root = discover_project()
    with target_lock(root, target):
        evaluated = evaluate(root, target, runner)
        spec = evaluated.spec
        if spec.user.uid != os.getuid():
            raise NixantError(
                f"set nixant.user.uid = {os.getuid()}; "
                f"configured UID is {spec.user.uid}"
            )
        sources = resolve_mount_sources(
            root, {mount.name: mount.source for mount in spec.mounts}
        )
        spec = replace(
            spec,
            mounts=tuple(
                replace(mount, source=str(sources[mount.name])) for mount in spec.mounts
            ),
        )
        if spec.kind != "container":
            raise NixantError("VM support is not implemented yet")
        if (
            spec.cpus is not None
            or spec.memory_bytes is not None
            or spec.disk_bytes is not None
            or spec.ports
        ):
            raise NixantError(
                "CPU/memory/disk limits and ports require Phase 2 support"
            )
        assert evaluated.drv_path is not None
        system = build(root, target, evaluated.drv_path, runner)
        state = resolve(provider, root, target, spec.instance_name, kind=spec.kind)
        if rebuild:
            if state is None or state.status != "Running":
                raise NixantError(
                    f"instance {spec.instance_name} is not running; run nixant up"
                )
            for mount in spec.mounts:
                actual = state.devices.get(f"nixant-mount-{mount.name}", {})
                if (
                    actual.get("source") != mount.source
                    or actual.get("path") != mount.target
                    or (actual.get("readonly", "false") == "true") != mount.read_only
                ):
                    typer.echo(
                        "warning: outer mount settings differ; run nixant up to apply",
                        err=True,
                    )
                    break
        else:
            if state is None:
                provider.create(spec, metadata(root, target))
                provider.start(spec.instance_name)
            elif state.status in ("Stopped", "Frozen"):
                for mount in spec.mounts:
                    provider.ensure_mount(spec.instance_name, mount)
                provider.start(spec.instance_name)
            elif state.status != "Running":
                raise NixantError(
                    f"instance {spec.instance_name} "
                    f"has unsupported state {state.status}"
                )
        wait_ready(provider, spec.instance_name, spec.kind, verbose=runner.verbose)
        if not rebuild:
            for mount in spec.mounts:
                provider.ensure_mount(spec.instance_name, mount, verify=True)
        if rebuild or state is None or not can_skip(provider, state, system):
            activate(provider, runner, spec, system, timeout=duration)
        final = provider.inspect(spec.instance_name)
        addresses = " ".join(final.ipv4) if final else ""
        typer.echo(
            f"{spec.instance_name} ready ({target}) {addresses}; nixant shell {target}"
        )


@app.command()
def up(
    ctx: typer.Context,
    target: str = typer.Argument("dev"),
    timeout: str | None = typer.Option(None, help="Activation deadline, e.g. 5m."),
) -> None:
    """Build, start and activate the target environment."""
    _deploy(ctx, target, timeout, rebuild=False)


@app.command()
def rebuild(
    ctx: typer.Context,
    target: str = typer.Argument("dev"),
    timeout: str | None = typer.Option(None, help="Activation deadline, e.g. 5m."),
) -> None:
    """Build and always activate an existing running environment."""
    _deploy(ctx, target, timeout, rebuild=True)

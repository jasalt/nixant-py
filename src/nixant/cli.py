"""Command-line entry point."""

import json
import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

import click
import typer
from typer.core import TyperGroup

from nixant.errors import CommandError, NixantError, UsageError
from nixant.init import (
    DEFAULT_TEMPLATE,
    init_project,
    list_templates,
    print_templates,
    self_path,
)
from nixant.models import SCHEMA_VERSION, MachineSpec, MachineState
from nixant.naming import (
    get_override,
    propose,
    set_override,
    unset_override,
    validate_instance_name,
)
from nixant.nix.activate import activate, can_skip
from nixant.nix.build import build, gcroot_path
from nixant.nix.eval import evaluate
from nixant.ownership import (
    PREFIX,
    check_owner,
    lookup,
    metadata,
    require_instance,
    resolve,
)
from nixant.planner import Change, Effect, plan
from nixant.project import (
    discover_project,
    project_id,
    resolve_mount_sources,
    target_lock,
)
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


def _apply(provider: IncusProvider, name: str, changes: list[Change]) -> None:
    for change in changes:
        typer.echo(f"{change.setting}: {change.summary}", err=True)
        provider.apply(name, change)


def _reconcile(provider: IncusProvider, spec: MachineSpec, state: MachineState) -> None:
    """Apply live changes now, restart-only ones when stopped, refuse the rest."""
    changes = plan(spec, state)
    blocked = [c for c in changes if c.effect in (Effect.RECREATE, Effect.UNSUPPORTED)]
    if blocked:
        raise NixantError(
            "cannot apply configuration to existing instance:\n"
            + "\n".join(f"  {c.setting}: {c.summary}" for c in blocked)
        )
    if any(c.op == "root-size" for c in changes):
        provider.check_quota(state.expanded_devices.get("root", {}).get("pool"))
    live = [c for c in changes if c.effect is Effect.LIVE]
    later = [c for c in changes if c.effect is Effect.RESTART]
    _apply(provider, spec.instance_name, live)
    if state.status == "Stopped":
        _apply(provider, spec.instance_name, later)
    else:
        for change in later:
            typer.echo(
                f"{change.setting}: {change.summary} needs a restart; "
                "run nixant restart",
                err=True,
            )


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
        override = get_override(root, target, runner)
        if override:
            spec = replace(spec, instance_name=override)
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
        assert evaluated.drv_path is not None
        system = build(root, target, evaluated.drv_path, runner)
        state = resolve(
            provider,
            root,
            target,
            spec.instance_name,
            kind=spec.kind,
            name_source="git override" if override else "config",
        )
        if rebuild:
            if state is None or state.status != "Running":
                raise NixantError(
                    f"instance {spec.instance_name} is not running; run nixant up"
                )
            outer = plan(spec, state)
            if outer:
                fields = ", ".join(sorted({change.setting for change in outer}))
                typer.echo(
                    f"warning: outer settings differ ({fields}); "
                    "run nixant up to apply",
                    err=True,
                )
        else:
            if state is None:
                if spec.disk_bytes is not None:
                    provider.check_quota(None)
                provider.create(spec, metadata(root, target))
                state = provider.inspect(spec.instance_name)
                if state is None:
                    raise NixantError(f"instance {spec.instance_name} disappeared")
                _reconcile(provider, spec, state)
                provider.start(spec.instance_name)
            elif state.status not in ("Running", "Stopped", "Frozen"):
                raise NixantError(
                    f"instance {spec.instance_name} "
                    f"has unsupported state {state.status}"
                )
            else:
                _reconcile(provider, spec, state)
                if state.status != "Running":
                    provider.start(spec.instance_name)
        wait_ready(provider, spec.instance_name, spec.kind, verbose=runner.verbose)
        if not rebuild:
            for mount in spec.mounts:
                provider.ensure_mount(spec.instance_name, mount, verify=True)
        if rebuild or state is None or not can_skip(provider, state, system):
            activate(provider, runner, spec, system, timeout=duration)
        else:
            # Settings that do not change the system closure still drive shell/exec.
            runtime = {
                PREFIX + "user": spec.user.name,
                PREFIX + "workdir": spec.workdir,
            }
            stale = {k: v for k, v in runtime.items() if state.config.get(k) != v}
            if stale:
                provider.set_metadata(spec.instance_name, stale)
        final = provider.inspect(spec.instance_name)
        addresses = " ".join(final.ipv4) if final else ""
        typer.echo(
            f"{spec.instance_name} ready ({target}) {addresses}; nixant shell {target}"
        )


@app.command()
def init(
    ctx: typer.Context,
    template: str = typer.Argument(DEFAULT_TEMPLATE),
    list_: bool = typer.Option(False, "--list", help="List available templates."),
) -> None:
    """Create flake.nix and a role module in the current directory."""
    runner = ctx.obj["runner"]
    if list_:
        print_templates(list_templates(self_path(os.environ), runner))
        return
    init_project(Path.cwd(), template, runner, IncusProvider(runner))


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


def _enter(ctx: typer.Context, target: str, command: list[str] | None) -> None:
    provider = IncusProvider(ctx.obj["runner"])
    state = require_instance(lookup(provider, discover_project(), target))
    if state.status != "Running":
        raise NixantError(f"instance {state.name} is not running; run nixant up")
    user = state.config.get(PREFIX + "user")
    workdir = state.config.get(PREFIX + "workdir")
    if not user or not workdir:
        raise NixantError("no completed activation yet; run nixant up")
    if command is None:
        # The outer login bash sets the NixOS environment; the inner bash execs
        # the user's passwd shell as a login shell, including fish and zsh.
        command = [
            "/run/current-system/sw/bin/bash",
            "-c",
            'exec -l "$(getent passwd "$(id -un)" | cut -d: -f7)"',
        ]
    argv = provider.exec_argv(state.name, command, user=user, cwd=workdir)
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        raise NixantError(f"cannot execute incus: {exc}") from exc


@app.command()
def shell(ctx: typer.Context, target: str = typer.Argument("dev")) -> None:
    """Enter the guest user's login shell without evaluating Nix."""
    _enter(ctx, target, None)


@app.command(
    "exec",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
def exec_command(
    ctx: typer.Context,
    command: Annotated[list[str], typer.Argument()],
    target: str = typer.Option("dev", "--target", "-n"),
) -> None:
    """Run a guest command, preserving its arguments and exit status."""
    _enter(ctx, target, command)


@app.command()
def down(
    ctx: typer.Context,
    target: str = typer.Argument("dev"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Stop an owned environment without evaluating its configuration."""
    root = discover_project()
    provider = IncusProvider(ctx.obj["runner"])
    with target_lock(root, target):
        state = require_instance(lookup(provider, root, target, require_schema=False))
        if state.status == "Stopped":
            typer.echo(f"{state.name}: already stopped")
            return
        if state.status == "Frozen" and not force:
            provider.start(state.name)
        try:
            provider.stop(state.name, force=force)
        except CommandError as exc:
            raise NixantError(
                f"could not stop {state.name} within 60s; "
                f"run nixant down {target} --force\n{exc}"
            ) from exc
        typer.echo(f"{state.name}: stopped")


@app.command()
def restart(
    ctx: typer.Context,
    target: str = typer.Argument("dev"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Restart (or start) an owned environment without re-activating it."""
    root = discover_project()
    runner = ctx.obj["runner"]
    provider = IncusProvider(runner)
    with target_lock(root, target):
        state = require_instance(lookup(provider, root, target, require_schema=False))
        if state.status in ("Stopped", "Frozen"):
            # Starting a frozen instance resumes it; a clean restart of one hangs.
            provider.start(state.name)
        if state.status != "Stopped":
            try:
                provider.restart(state.name, force=force)
            except CommandError as exc:
                raise NixantError(
                    f"could not restart {state.name} within 60s; "
                    f"run nixant restart {target} --force\n{exc}"
                ) from exc
        kind = "vm" if state.kind == "virtual-machine" else "container"
        wait_ready(provider, state.name, kind, verbose=runner.verbose)
        typer.echo(f"{state.name}: restarted")


@app.command()
def destroy(ctx: typer.Context, target: str = typer.Argument("dev")) -> None:
    """Delete an owned environment and its disposable host GC root."""
    root = discover_project()
    provider = IncusProvider(ctx.obj["runner"])
    with target_lock(root, target):
        state = lookup(provider, root, target, require_schema=False)
        if state is None:
            typer.echo(f"{target}: not created")
        else:
            provider.destroy(state.name)
            typer.echo(f"{state.name}: destroyed")
        try:
            gcroot_path(root, target).unlink(missing_ok=True)
        except OSError as exc:
            raise NixantError(f"cannot remove GC root: {exc}") from exc


@app.command()
def status(
    ctx: typer.Context,
    target: str | None = typer.Argument(None),
    orphans: bool = typer.Option(
        False, "--orphans", help="List managed instances whose checkout is gone."
    ),
) -> None:
    """Show metadata and cached build status without evaluating Nix."""
    provider = IncusProvider(ctx.obj["runner"])
    if orphans:
        if target is not None:
            raise UsageError("--orphans lists the whole host; do not pass a target")
        _orphans(provider)
        return
    root = discover_project()
    runner = ctx.obj["runner"]
    if target is not None:
        state = lookup(provider, root, target, require_schema=False)
        states = [state] if state else []
    else:
        states = provider.find(
            {PREFIX + "managed": "true", PREFIX + "project": project_id(root)}
        )
    if not states:
        typer.echo(f"{target or 'project'}: not created")
    for state in states:
        recorded_target = state.config.get(PREFIX + "target", "")
        check_owner(state, root, recorded_target)
        system = state.config.get(PREFIX + "system", "none")
        activation = state.config.get(PREFIX + "activation", "none")
        link = gcroot_path(root, recorded_target)
        current = "unknown"
        if link.is_symlink():
            current = "current" if str(link.resolve()) == system else "outdated"
        schema = state.config.get(PREFIX + "schema", "missing")
        mismatch = (
            f"; schema mismatch ({schema} != {SCHEMA_VERSION})"
            if schema != str(SCHEMA_VERSION)
            else ""
        )
        marker = (
            " (override)"
            if get_override(root, recorded_target, runner) == state.name
            else ""
        )
        typer.echo(
            f"{state.name}{marker}  {state.status.upper()}  "
            f"{state.kind}  {' '.join(state.ipv4)}"
        )
        typer.echo(
            f"target: {recorded_target}  system: {system} "
            f"({activation}, {current}{mismatch})"
        )
        typer.echo(f"root: {state.config.get(PREFIX + 'root', 'unknown')}")


def _orphans(provider: IncusProvider) -> None:
    states = provider.find({PREFIX + "managed": "true"})
    orphans = [
        state
        for state in states
        if not Path(state.config.get(PREFIX + "root", "")).is_dir()
    ]
    if not orphans:
        typer.echo("no orphaned instances")
        return
    for state in orphans:
        typer.echo(
            f"{state.name}  target: {state.config.get(PREFIX + 'target', '?')}  "
            f"root: {state.config.get(PREFIX + 'root', 'unknown')}"
        )
    typer.echo(
        "remove with `incus delete --force local:NAME`, "
        "or run `nixant adopt` in the new checkout"
    )


@app.command("config")
def show_config(ctx: typer.Context, target: str = typer.Argument("dev")) -> None:
    """Print runtime configuration and resolved checkout paths as JSON."""
    root = discover_project()
    spec = evaluate(root, target, ctx.obj["runner"], with_derivation=False).spec
    override = get_override(root, target, ctx.obj["runner"])
    sources = resolve_mount_sources(
        root, {mount.name: mount.source for mount in spec.mounts}
    )
    typer.echo(
        json.dumps(
            {
                "runtime": spec.to_runtime(),
                "projectRoot": str(root),
                "projectId": project_id(root),
                "instanceName": override or spec.instance_name,
                "instanceNameSource": "git override" if override else "config",
                "mountSources": {name: str(path) for name, path in sources.items()},
            },
            indent=2,
        )
    )


@app.command()
def name(
    ctx: typer.Context,
    target: str = typer.Argument("dev"),
    new_name: str | None = typer.Argument(None, metavar="[NAME]"),
    unset: bool = typer.Option(False, "--unset", help="Remove the override."),
) -> None:
    """Give this checkout its own instance name for a target."""
    root = discover_project()
    runner = ctx.obj["runner"]
    if unset and new_name is not None:
        raise UsageError("pass either NAME or --unset, not both")
    provider = IncusProvider(runner)
    with target_lock(root, target):
        existing = lookup(provider, root, target, require_schema=False)
        if existing is not None:
            raise NixantError(
                f"instance {existing.name} exists for target {target}; "
                f"destroy it first, or keep the current name"
            )
        if unset:
            scope = unset_override(root, target, runner)
            typer.echo(
                f"removed the {scope} override for {target}"
                if scope
                else f"no override set for {target}"
            )
            return
        if new_name is None:
            committed = evaluate(root, target, runner, with_derivation=False)
            new_name = propose(committed.spec.instance_name, root)
        validate_instance_name(new_name)
        scope = set_override(root, target, new_name, runner)
        typer.echo(
            f"{target} now uses instance name {new_name} (git config, {scope} scope)"
        )

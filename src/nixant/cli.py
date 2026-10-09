"""Command-line entry point."""

import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import click
import typer
from typer.core import TyperGroup

from nixant import deploy, snapshots
from nixant.adopt import adopt as adopt_instance
from nixant.adopt import choose
from nixant.errors import CommandError, NixantError, UsageError
from nixant.incus import IncusProvider
from nixant.init import (
    DEFAULT_TEMPLATE,
    init_project,
    list_templates,
    print_templates,
    self_path,
)
from nixant.models import SCHEMA_VERSION
from nixant.naming import (
    get_override,
    propose,
    set_override,
    unset_override,
    validate_instance_name,
)
from nixant.nix.build import gcroot_path
from nixant.nix.eval import evaluate_spec
from nixant.ownership import (
    PREFIX,
    check_owner,
    guest_dir,
    lookup,
    require_instance,
    target_at,
)
from nixant.project import (
    discover_project,
    project_id,
    resolve_mount_sources,
    target_lock,
)
from nixant.proxy import command_routes, local_routes, serve, setup_help
from nixant.readiness import wait_ready
from nixant.run import Runner, check_host_tools


class ErrorHandlingGroup(TyperGroup):
    """Translate expected failures at the command boundary only."""

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        # The group callback runs before the subcommand parses its own --help,
        # so record here whether the subcommand's parser will show help.
        name, command, rest = super().resolve_command(ctx, args)
        ctx.meta["nixant.help"] = command is not None and _wants_help(
            command, ctx, rest
        )
        return name, command, rest

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


def _wants_help(command: click.Command, ctx: click.Context, args: list[str]) -> bool:
    """Whether args request help, honoring `--` and commands like exec."""
    try:
        options, _, _ = command.make_parser(ctx).parse_args(list(args))
    except click.UsageError:
        return False
    return bool(options.get("help"))


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Print commands before running."
    ),
) -> None:
    """Manage local NixOS development environments."""
    if not ctx.meta.get("nixant.help"):
        check_host_tools()
    ctx.ensure_object(dict)
    ctx.obj["runner"] = Runner(verbose=verbose)


# A stuck switch-to-configuration never returns on its own (nixant-1va.3).
DEFAULT_ACTIVATION_TIMEOUT = 1800.0


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
    ctx: typer.Context,
    target: str,
    timeout: str | None,
    step: Callable[[IncusProvider, Runner, Path, str, float], None],
) -> None:
    duration = parse_duration(timeout) or DEFAULT_ACTIVATION_TIMEOUT
    runner = ctx.obj["runner"]
    root = discover_project()
    with target_lock(root, target):
        step(IncusProvider(runner), runner, root, target, duration)


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
    timeout: str | None = typer.Option(
        None, help="Activation deadline, e.g. 5m (default 30m)."
    ),
) -> None:
    """Build, start and activate the target environment."""
    _deploy(ctx, target, timeout, deploy.up)


@app.command()
def rebuild(
    ctx: typer.Context,
    target: str = typer.Argument("dev"),
    timeout: str | None = typer.Option(None, help="Activation deadline, e.g. 5m."),
) -> None:
    """Build and always activate an existing running environment."""
    _deploy(ctx, target, timeout, deploy.rebuild)


def _enter(ctx: typer.Context, target: str | None, command: list[str] | None) -> None:
    provider = IncusProvider(ctx.obj["runner"])
    root = discover_project()
    if target is None:
        target = target_at(provider, root, Path.cwd()) or "dev"
    state = require_instance(lookup(provider, root, target), provider, root, target)
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
    # Inside a mount, start where the current directory appears in the guest.
    cwd = guest_dir(state, Path.cwd()) or workdir
    argv = provider.exec_argv(state.name, command, user=user, cwd=cwd)
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        raise NixantError(f"cannot execute incus: {exc}") from exc


@app.command()
def shell(
    ctx: typer.Context,
    target: str | None = typer.Argument(
        None, help="Default: the target mounting this directory, else dev."
    ),
) -> None:
    """Enter the guest user's login shell without evaluating Nix."""
    _enter(ctx, target, None)


# For Vagrant habits; like shell it runs incus exec, not SSH.
app.command("ssh", help="Alias of shell (incus exec, not SSH).")(shell)


@app.command(
    "exec",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
def exec_command(
    ctx: typer.Context,
    command: Annotated[list[str], typer.Argument()],
    target: str | None = typer.Option(
        None,
        "--target",
        "-n",
        help="Default: the target mounting this directory, else dev.",
    ),
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
        state = require_instance(
            lookup(provider, root, target, require_schema=False), provider, root, target
        )
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
        if state.ephemeral:
            # Incus deletes ephemeral instances on stop; drop the build cache too.
            _remove_gcroot(root, target)
            typer.echo(f"{state.name}: stopped and deleted (ephemeral)")
            return
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
        state = require_instance(
            lookup(provider, root, target, require_schema=False), provider, root, target
        )
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
        wait_ready(provider, state.name, state.kind, verbose=runner.verbose)
        typer.echo(f"{state.name}: restarted")


def _remove_gcroot(root: Path, target: str) -> None:
    try:
        gcroot_path(root, target).unlink(missing_ok=True)
    except OSError as exc:
        raise NixantError(f"cannot remove GC root: {exc}") from exc


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
        _remove_gcroot(root, target)


@app.command()
def proxy(
    ctx: typer.Context,
    port: int = typer.Option(80, "--port", help="Loopback port to serve on."),
    print_setup: bool = typer.Option(
        False, "--print-setup", help="Show how to allow binding the port, then exit."
    ),
    routes: bool = typer.Option(
        False, "--routes", help="Print the route table as JSON, then exit."
    ),
    routes_from: str | None = typer.Option(
        None,
        "--routes-from",
        metavar="COMMAND",
        help="Take routes from COMMAND's output (another host's `proxy --routes`, "
        "e.g. `limactl shell default nixant proxy --routes`), polling it.",
    ),
) -> None:
    """Serve <name>.localhost for every running instance's hostname forwards.

    Runs Caddy on 127.0.0.1 and [::1] only and keeps its routes in step with
    Incus. It changes nothing on the host; --print-setup explains port 80.
    """
    if print_setup:
        typer.echo(setup_help(port))
        return
    provider = IncusProvider(ctx.obj["runner"])
    if routes:
        typer.echo(json.dumps(local_routes(provider), indent=2, sort_keys=True))
        return
    if routes_from is not None:
        argv = shlex.split(routes_from)
        serve(lambda: command_routes(argv), port=port, watch_incus=False)
        return
    serve(lambda: local_routes(provider), port=port)


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
            f"{'ephemeral ' if state.ephemeral else ''}"
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
    spec = evaluate_spec(root, target, ctx.obj["runner"])
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
            committed = evaluate_spec(root, target, runner)
            new_name = propose(committed.instance_name, root)
        validate_instance_name(new_name)
        scope = set_override(root, target, new_name, runner)
        typer.echo(
            f"{target} now uses instance name {new_name} (git config, {scope} scope)"
        )


@app.command()
def adopt(
    ctx: typer.Context,
    target: str = typer.Argument("dev"),
    instance: str | None = typer.Option(
        None, "--instance", help="Instance to adopt when several match."
    ),
) -> None:
    """Attach an instance whose checkout moved to this checkout."""
    root = discover_project()
    runner = ctx.obj["runner"]
    provider = IncusProvider(runner)
    with target_lock(root, target):
        current = lookup(provider, root, target, require_schema=False)
        if current is not None:
            typer.echo(f"{current.name}: already belongs to this checkout")
            return
        state = choose(provider, root, target, instance)
        adopt_instance(provider, runner, root, target, state)
        typer.echo(f"{state.name}: adopted by {root}")


@app.command("snapshot")
def snapshot_command(
    ctx: typer.Context,
    name: str | None = typer.Argument(None, help="Snapshot name (default: timestamp)."),
    target: str = typer.Option("dev", "--target", "-t"),
    delete: bool = typer.Option(False, "--delete", help="Delete NAME instead."),
) -> None:
    """Snapshot an owned environment, or delete a snapshot."""
    root = discover_project()
    provider = IncusProvider(ctx.obj["runner"])
    with target_lock(root, target):
        state = require_instance(
            lookup(provider, root, target, require_schema=False), provider, root, target
        )
        if delete:
            if name is None:
                raise UsageError("--delete needs a snapshot NAME")
            snapshots.find(provider, state, name)
            provider.snapshot_delete(state.name, name)
            typer.echo(f"{state.name}: deleted snapshot {name}")
            return
        chosen = snapshots.validate_name(name or snapshots.default_name())
        snapshots.create(provider, state, chosen)
        typer.echo(f"{state.name}: created snapshot {chosen}")


@app.command("snapshots")
def snapshots_command(
    ctx: typer.Context, target: str = typer.Option("dev", "--target", "-t")
) -> None:
    """List the snapshots of an owned environment."""
    root = discover_project()
    provider = IncusProvider(ctx.obj["runner"])
    state = require_instance(
        lookup(provider, root, target, require_schema=False), provider, root, target
    )
    found = provider.snapshot_list(state.name)
    if not found:
        typer.echo(f"{state.name}: no snapshots")
    for item in found:
        typer.echo(f"{item.name}  {item.created_at}")


@app.command()
def restore(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Snapshot to roll back to."),
    target: str = typer.Option("dev", "--target", "-t"),
) -> None:
    """Roll an owned environment back to a snapshot."""
    root = discover_project()
    runner = ctx.obj["runner"]
    provider = IncusProvider(runner)
    with target_lock(root, target):
        state = require_instance(
            lookup(provider, root, target, require_schema=False), provider, root, target
        )
        snapshots.restore(provider, state, name, root)
        after = provider.inspect(state.name)
        if after is not None and after.status == "Running":
            wait_ready(provider, state.name, after.kind, verbose=runner.verbose)
        typer.echo(
            f"{state.name}: restored snapshot {name}; "
            "run nixant up to reconcile the configuration"
        )

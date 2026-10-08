"""up and rebuild: evaluate, build, converge the instance, then activate."""

import os
from dataclasses import dataclass, replace
from pathlib import Path

import typer

from nixant.errors import NixantError
from nixant.incus import IncusProvider
from nixant.models import MachineSpec, MachineState
from nixant.naming import get_override
from nixant.nix.activate import activate, can_skip
from nixant.nix.build import build
from nixant.nix.eval import evaluate
from nixant.ownership import PREFIX, metadata, resolve
from nixant.planner import (
    Change,
    Effect,
    SetRootSize,
    check_mount,
    plan,
    validate,
)
from nixant.project import resolve_mount_sources
from nixant.readiness import wait_ready
from nixant.run import Runner


@dataclass(frozen=True)
class Desired:
    """The evaluated configuration, adjusted for this checkout and host."""

    spec: MachineSpec
    drv_path: str
    name_source: str


def up(
    provider: IncusProvider, runner: Runner, root: Path, target: str, timeout: float
) -> None:
    desired = _desired(root, target, runner)
    spec = desired.spec
    # Fail before building or creating anything the instance cannot take.
    _refuse(validate(spec), "cannot apply configuration")
    system = build(root, target, desired.drv_path, runner)
    current = _resolve(provider, root, target, desired)
    state = _converge(provider, root, target, spec, current)
    wait_ready(provider, spec.instance_name, spec.kind, verbose=runner.verbose)
    provider.verify_mounts(spec.instance_name, spec.mounts)
    if can_skip(provider, state, system):
        _refresh_runtime(provider, spec, state)
    else:
        activate(provider, runner, spec, system, timeout=timeout)
    _report(provider, spec, target)


def rebuild(
    provider: IncusProvider, runner: Runner, root: Path, target: str, timeout: float
) -> None:
    desired = _desired(root, target, runner)
    spec = desired.spec
    system = build(root, target, desired.drv_path, runner)
    state = _resolve(provider, root, target, desired)
    if state is None or state.status != "Running":
        raise NixantError(
            f"instance {spec.instance_name} is not running; run nixant up"
        )
    outer = plan(spec, state)
    if outer:
        fields = ", ".join(sorted({change.setting for change in outer}))
        typer.echo(
            f"warning: outer settings differ ({fields}); run nixant up to apply",
            err=True,
        )
    wait_ready(provider, spec.instance_name, spec.kind, verbose=runner.verbose)
    activate(provider, runner, spec, system, timeout=timeout)
    _report(provider, spec, target)


def _desired(root: Path, target: str, runner: Runner) -> Desired:
    evaluated = evaluate(root, target, runner)
    spec = evaluated.spec
    override = get_override(root, target, runner)
    if override:
        spec = replace(spec, instance_name=override)
    if spec.user.uid != os.getuid():
        raise NixantError(
            f"set nixant.user.uid = {os.getuid()}; configured UID is {spec.user.uid}"
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
    for mount in spec.mounts:
        check_mount(mount)
    return Desired(spec, evaluated.drv_path, "git override" if override else "config")


def _resolve(
    provider: IncusProvider, root: Path, target: str, desired: Desired
) -> MachineState | None:
    return resolve(
        provider,
        root,
        target,
        desired.spec.instance_name,
        kind=desired.spec.kind,
        name_source=desired.name_source,
    )


def _converge(
    provider: IncusProvider,
    root: Path,
    target: str,
    spec: MachineSpec,
    state: MachineState | None,
) -> MachineState:
    """Create or reconcile the instance and leave it running."""
    if state is None:
        return _create(provider, root, target, spec)
    if state.status not in ("Running", "Stopped", "Frozen"):
        raise NixantError(
            f"instance {spec.instance_name} has unsupported state {state.status}"
        )
    _reconcile(provider, spec, state)
    if state.status != "Running":
        provider.start(spec.instance_name)
    return state


def _create(
    provider: IncusProvider, root: Path, target: str, spec: MachineSpec
) -> MachineState:
    if spec.disk_bytes is not None:
        provider.check_quota(None)
    provider.create(spec, metadata(root, target))
    state = provider.inspect(spec.instance_name)
    if state is None:
        raise NixantError(f"instance {spec.instance_name} disappeared")
    _reconcile(provider, spec, state)
    provider.start(spec.instance_name)
    return state


def _reconcile(provider: IncusProvider, spec: MachineSpec, state: MachineState) -> None:
    """Store every change on the instance, or refuse the whole set up front."""
    changes = plan(spec, state)
    _refuse(changes, "cannot apply configuration to existing instance")
    if any(isinstance(c.action, SetRootSize) for c in changes):
        provider.check_quota(state.expanded_devices.get("root", {}).get("pool"))
    # Restart-effect settings are stored now (Incus accepts them on a running
    # instance) but only show up in the guest after its next boot.
    for change in changes:
        typer.echo(f"{change.setting}: {change.summary}", err=True)
        provider.apply(spec.instance_name, change)
    if state.status != "Stopped":
        for change in changes:
            if change.effect is Effect.RESTART:
                typer.echo(
                    f"{change.setting}: takes effect after the next restart; "
                    f"run nixant restart",
                    err=True,
                )


def _refuse(changes: list[Change], reason: str) -> None:
    blocked = [c for c in changes if c.effect in (Effect.RECREATE, Effect.UNSUPPORTED)]
    if blocked:
        raise NixantError(
            f"{reason}:\n" + "\n".join(f"  {c.setting}: {c.summary}" for c in blocked)
        )


def _refresh_runtime(
    provider: IncusProvider, spec: MachineSpec, state: MachineState
) -> None:
    """Settings that do not change the system closure still drive shell/exec."""
    runtime = {PREFIX + "user": spec.user.name, PREFIX + "workdir": spec.workdir}
    stale = {k: v for k, v in runtime.items() if state.config.get(k) != v}
    if stale:
        provider.set_metadata(spec.instance_name, stale)


def _report(provider: IncusProvider, spec: MachineSpec, target: str) -> None:
    final = provider.inspect(spec.instance_name)
    addresses = " ".join(final.ipv4) if final else ""
    typer.echo(
        f"{spec.instance_name} ready ({target}) {addresses}; nixant shell {target}"
    )

"""Ownership is the managed/project/target triple, never just a name."""

from pathlib import Path

from nixant.errors import NixantError
from nixant.incus import IncusProvider
from nixant.models import SCHEMA_VERSION, MachineState
from nixant.project import project_id, validate_target

PREFIX = "user.nixant."


def metadata(root: Path, target: str) -> dict[str, str]:
    validate_target(target)
    return {
        PREFIX + "managed": "true",
        PREFIX + "project": project_id(root),
        PREFIX + "root": str(root.resolve()),
        PREFIX + "target": target,
        PREFIX + "schema": str(SCHEMA_VERSION),
    }


def check_owner(state: MachineState, root: Path, target: str) -> None:
    config = state.config
    if config.get(PREFIX + "managed") != "true":
        raise NixantError(f"instance {state.name} exists but is not managed by nixant")
    if config.get(PREFIX + "project") != project_id(root):
        raise NixantError(
            f"instance {state.name} belongs to "
            f"{config.get(PREFIX + 'root', 'another checkout')}; "
            f"for a second checkout run `nixant name {target}`, or `nixant adopt` "
            "if that checkout was moved"
        )
    if config.get(PREFIX + "target") != target:
        raise NixantError(
            f"instance {state.name} belongs to target {config.get(PREFIX + 'target')} "
            f"of this project; give {target} a different nixant.instanceName"
        )


def check_schema(state: MachineState) -> None:
    schema = state.config.get(PREFIX + "schema", "missing")
    if schema != str(SCHEMA_VERSION):
        raise NixantError(
            f"instance {state.name} uses nixant schema {schema}, "
            f"this CLI uses {SCHEMA_VERSION}; "
            "destroy and recreate it, or use a matching nixant version"
        )


def lookup(
    provider: IncusProvider, root: Path, target: str, *, require_schema: bool = True
) -> MachineState | None:
    validate_target(target)
    states = provider.find(
        {PREFIX + "project": project_id(root), PREFIX + "target": target}
    )
    if len(states) > 1:
        names = ", ".join(sorted(state.name for state in states))
        raise NixantError(
            f"multiple instances match target {target}: {names}; "
            "use incus delete local:NAME to remove the stale instance"
        )
    if not states:
        return None
    state = states[0]
    # Re-check returned data, even though the CLI query also filters it.
    check_owner(state, root, target)
    if require_schema:
        check_schema(state)
    return state


def resolve(
    provider: IncusProvider,
    root: Path,
    target: str,
    name: str,
    *,
    kind: str,
    name_source: str = "config",
) -> MachineState | None:
    state = lookup(provider, root, target)
    if state is not None and state.name != name:
        raise NixantError(
            f"target {target}'s instance is {state.name} "
            f"but the {name_source} now names {name}; "
            f"run nixant destroy {target} "
            "or restore nixant.instanceName/the git override"
        )
    if state is None:
        state = provider.inspect(name)
        if state is None:
            return None
        check_owner(state, root, target)
        check_schema(state)
    if state.kind != kind:
        raise NixantError(
            f"instance {name} is {state.kind}, not {kind}; "
            "destroy and up to change kind"
        )
    return state


def require_instance(
    state: MachineState | None, provider: IncusProvider, root: Path, target: str
) -> MachineState:
    if state is not None:
        return state
    found = provider.find(
        {PREFIX + "managed": "true", PREFIX + "project": project_id(root)}
    )
    others = sorted({item.config.get(PREFIX + "target", "") for item in found} - {""})
    if not others:
        raise NixantError(f"target {target} does not exist; run nixant up {target}")
    raise NixantError(
        f"target {target} does not exist; this project has: {', '.join(others)}. "
        f"Name one of them as the target, or run nixant up {target}"
    )

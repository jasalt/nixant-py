"""Evaluate runtime and optionally the system derivation in one Nix invocation."""

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from nixant.errors import NixantError, UsageError
from nixant.models import MachineSpec
from nixant.nix.store import is_drv_path
from nixant.project import evaluation_preflight, validate_target
from nixant.run import Runner


@dataclass(frozen=True)
class Evaluation:
    spec: MachineSpec
    drv_path: str


def evaluate(root: Path, target: str, runner: Runner) -> Evaluation:
    """The runtime and the system derivation, from one evaluation."""
    spec, drv_path = _evaluate(root, target, runner, with_derivation=True)
    return Evaluation(spec, _drv_path(drv_path))


def evaluate_spec(root: Path, target: str, runner: Runner) -> MachineSpec:
    """The runtime only; cheaper, since nothing forces the system closure."""
    return _evaluate(root, target, runner, with_derivation=False)[0]


def _drv_path(value: object) -> str:
    if not is_drv_path(value):
        raise NixantError(
            "invalid Nix evaluation response: invalid system derivation path"
        )
    return value


def _evaluate(
    root: Path, target: str, runner: Runner, *, with_derivation: bool
) -> tuple[MachineSpec, object]:
    validate_target(target)
    evaluation_preflight(root, runner)
    print(f"evaluating {target}…", file=sys.stderr, flush=True)
    # Apply to the configuration set so missing targets and missing modules can
    # be diagnosed in the same evaluation, without guessing from Nix stderr.
    expression = (
        f'cs: if !(builtins.hasAttr "{target}" cs) then '
        '{ error = "target"; targets = builtins.attrNames cs; } '
        f"else let c = cs.{target}.config; in "
        'if !(c.nixant.enable or false) then { error = "module"; } '
        "else { runtime = c.nixant.runtime; "
        + ("drvPath = c.system.build.toplevel.drvPath; " if with_derivation else "")
        + "}"
    )
    result = runner.run(
        ["nix", "eval", "--json", ".#nixosConfigurations", "--apply", expression],
        cwd=root,
        capture=True,
    )
    try:
        data = json.loads(result.stdout)
        if data.get("error") == "target":
            raise UsageError(
                f"target {target!r} not found; available targets: "
                + (", ".join(data["targets"]) or "(none)")
            )
        if data.get("error") == "module":
            raise NixantError(
                f"target {target!r} must import nixant.nixosModules.container"
            )
        spec = MachineSpec.from_runtime(data["runtime"])
        return spec, data["drvPath"] if with_derivation else None
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise NixantError(f"invalid Nix evaluation response: {exc}") from exc

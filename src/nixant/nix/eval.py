"""Evaluate runtime and optionally the system derivation in one Nix invocation."""

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from nixant.errors import NixantError, UsageError
from nixant.models import MachineSpec
from nixant.project import evaluation_preflight, validate_target
from nixant.run import Runner


@dataclass(frozen=True)
class Evaluation:
    spec: MachineSpec
    drv_path: str | None


def evaluate(
    root: Path, target: str, runner: Runner, *, with_derivation: bool = True
) -> Evaluation:
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
        drv_path = data["drvPath"] if with_derivation else None
        if with_derivation and (
            not isinstance(drv_path, str)
            or re.fullmatch(r"/nix/store/[a-z0-9]{32}-[^/\s]+\.drv", drv_path) is None
        ):
            raise ValueError("invalid system derivation path")
        return Evaluation(spec, drv_path)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise NixantError(f"invalid Nix evaluation response: {exc}") from exc

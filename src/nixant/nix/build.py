"""Build evaluated derivations with a per-target, disposable GC root."""

import json
import re
from pathlib import Path

from nixant.errors import NixantError
from nixant.project import project_id, state_directory, validate_target
from nixant.run import Runner


def gcroot_path(root: Path, target: str) -> Path:
    validate_target(target)
    return state_directory() / "gcroots" / f"{project_id(root)}-{target}"


def build(root: Path, target: str, drv_path: str, runner: Runner) -> str:
    if re.fullmatch(r"/nix/store/[a-z0-9]{32}-[^/\s]+\.drv", drv_path) is None:
        raise NixantError("invalid system derivation path")
    link = gcroot_path(root, target)
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise NixantError(
            f"cannot create GC root directory {link.parent}: {exc}"
        ) from exc
    result = runner.run(
        ["nix", "build", f"{drv_path}^out", "--out-link", str(link), "--json"],
        cwd=root,
        capture=True,
    )
    try:
        outputs = json.loads(result.stdout)
        if len(outputs) != 1:
            raise ValueError("expected exactly one system output")
        path = outputs[0]["outputs"]["out"]
        if (
            not isinstance(path, str)
            or re.fullmatch(r"/nix/store/[a-z0-9]{32}-[^/\s]+", path) is None
        ):
            raise ValueError("invalid system output path")
        return path
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise NixantError(f"invalid Nix build response: {exc}") from exc

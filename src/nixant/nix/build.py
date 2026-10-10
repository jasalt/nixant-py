"""Build evaluated derivations with a per-target, disposable GC root."""

import json
from pathlib import Path

from nixant.errors import NixantError
from nixant.nix.store import is_drv_path, is_store_path
from nixant.project import project_id, state_directory, validate_target
from nixant.run import Runner


def gcroot_path(root: Path, target: str) -> Path:
    validate_target(target)
    return state_directory() / "gcroots" / f"{project_id(root)}-{target}"


def build(
    root: Path, target: str, drv_path: str, runner: Runner, *, root_it: bool = True
) -> str:
    """Without root_it the GC root keeps naming the last deployed system."""
    if not is_drv_path(drv_path):
        raise NixantError("invalid system derivation path")
    link = gcroot_path(root, target)
    if root_it:
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise NixantError(
                f"cannot create GC root directory {link.parent}: {exc}"
            ) from exc
    out = ["--out-link", str(link)] if root_it else ["--no-link"]
    result = runner.run(
        ["nix", "build", f"{drv_path}^out", *out, "--json"],
        cwd=root,
        capture=True,
    )
    try:
        outputs = json.loads(result.stdout)
        if len(outputs) != 1:
            raise ValueError("expected exactly one system output")
        path = outputs[0]["outputs"]["out"]
        if not is_store_path(path):
            raise ValueError("invalid system output path")
        return path
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise NixantError(f"invalid Nix build response: {exc}") from exc

"""Workspace discovery, isolated configuration sources, and host-side locks."""

import fcntl
import hashlib
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from nixant.errors import NixantError, UsageError


def config_directory(root: Path) -> Path:
    """Configuration source; the workspace remains the ownership identity."""
    return root / ".nixant"


def flake_reference(root: Path) -> str:
    """Force path semantics, even inside a larger Git work tree."""
    return "path:" + quote(str(config_directory(root).resolve()), safe="/")


def discover_project(start: Path | None = None) -> Path:
    """Find the nearest enclosing .nixant/flake.nix, resolving symlink aliases."""
    directory = (start if start is not None else Path.cwd()).resolve()
    legacy = None
    for candidate in (directory, *directory.parents):
        if (config_directory(candidate) / "flake.nix").is_file():
            return candidate
        if legacy is None and (candidate / "flake.nix").is_file():
            legacy = candidate
    if legacy is not None:
        raise NixantError(
            f"found root-level flake at {legacy}; migrate the environment "
            "configuration to .nixant/ (see docs/migration.md)"
        )
    raise NixantError("no .nixant/flake.nix found; run nixant init in the workspace")


def project_id(root: Path) -> str:
    return hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:12]


def validate_target(target: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", target) is None:
        raise UsageError(f"invalid target {target!r}; expected [A-Za-z_][A-Za-z0-9_-]*")
    return target


def resolve_mount_sources(root: Path, sources: Mapping[str, str]) -> dict[str, Path]:
    """Mount strings are relative to the workspace, never the config directory."""
    paths: dict[str, Path] = {}
    for name, source in sources.items():
        path = (root / source).resolve()
        if not path.exists():
            raise NixantError(f"nixant.mounts.{name}.source does not exist: {path}")
        paths[name] = path
    return paths


def state_directory() -> Path:
    """Ignore relative XDG paths, as required by the XDG Base Directory spec."""
    value = os.environ.get("XDG_STATE_HOME", "")
    base = (
        Path(value)
        if value and Path(value).is_absolute()
        else Path.home() / ".local/state"
    )
    return base / "nixant"


@contextmanager
def target_lock(root: Path, target: str) -> Iterator[None]:
    """Hold a nonblocking exclusive lock; never unlink the shared lock inode."""
    validate_target(target)
    path = state_directory() / "locks" / f"{project_id(root)}-{target}.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a+b")
    except OSError as exc:
        raise NixantError(f"cannot open target lock {path}: {exc}") from exc
    with stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise NixantError(
                f"another nixant command is running for target {target}"
            ) from exc
        except OSError as exc:
            raise NixantError(f"cannot acquire target lock {path}: {exc}") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)

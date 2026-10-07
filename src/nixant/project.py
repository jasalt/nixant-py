"""Checkout discovery, evaluation pre-flight, and host-side target locks."""

import fcntl
import hashlib
import os
import re
import shlex
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from nixant.errors import NixantError, UsageError
from nixant.run import Runner


def discover_project(start: Path | None = None) -> Path:
    """Find the nearest enclosing flake, resolving symlink aliases first."""
    directory = (start if start is not None else Path.cwd()).resolve()
    for candidate in (directory, *directory.parents):
        if (candidate / "flake.nix").is_file():
            return candidate
    raise NixantError("no flake.nix found in this directory or its parents")


def project_id(root: Path) -> str:
    return hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:12]


def validate_target(target: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", target) is None:
        raise UsageError(f"invalid target {target!r}; expected [A-Za-z_][A-Za-z0-9_-]*")
    return target


def resolve_mount_sources(root: Path, sources: Mapping[str, str]) -> dict[str, Path]:
    """Resolve all sources before any Incus mutation; files are valid mounts too."""
    resolved: dict[str, Path] = {}
    for name, source in sources.items():
        path = (root / source).resolve()
        if not path.exists():
            raise NixantError(f"nixant.mounts.{name}.source does not exist: {path}")
        resolved[name] = path
    return resolved


def in_git_work_tree(root: Path, runner: Runner) -> bool:
    result = runner.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        capture=True,
        capture_stderr=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == b"true"


def evaluation_preflight(root: Path, runner: Runner) -> None:
    """Warn once per invocation; callers invoke once before each evaluation."""
    if not in_git_work_tree(root, runner):
        print(
            "warning: outside a git work tree, Nix will copy the entire directory "
            "into the store on every evaluation; consider git init",
            file=sys.stderr,
        )
        return
    result = runner.run(
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "*.nix",
            "flake.lock",
        ],
        cwd=root,
        capture=True,
    )
    paths = [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]
    if paths:
        print("warning: Nix ignores files not tracked by git:", file=sys.stderr)
        for path in paths:
            print(f"  {shlex.quote(path)}", file=sys.stderr)
        print(f"run: {shlex.join(['git', 'add', '--', *paths])}", file=sys.stderr)


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

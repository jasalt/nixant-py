"""Per-checkout instance-name overrides, stored in the checkout's git config."""

import re
from pathlib import Path

from nixant.errors import NixantError, UsageError
from nixant.project import in_git_work_tree, validate_target
from nixant.run import Runner

NAME_PATTERN = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")


def validate_instance_name(name: str) -> str:
    if NAME_PATTERN.fullmatch(name) is None:
        raise UsageError(
            f"invalid instance name {name!r}: use at most 63 characters of "
            "[a-z0-9-], starting with a letter and not ending with a dash"
        )
    return name


def config_key(target: str) -> str:
    validate_target(target)
    return f"nixant.{target}.instanceName"


def sanitize(text: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return re.sub(r"^[0-9-]+", "", base).strip("-")


def propose(committed: str, checkout: Path) -> str:
    """<committed>-<checkout dir>, trimmed to a valid Incus name."""
    suffix = sanitize(checkout.name) or "checkout"
    head = committed[: max(1, 62 - len(suffix))].rstrip("-")
    return validate_instance_name(f"{head}-{suffix}"[:63].rstrip("-"))


def get_override(root: Path, target: str, runner: Runner) -> str | None:
    """The checkout's override for this target, or None (also outside git)."""
    if not in_git_work_tree(root, runner):
        return None
    result = runner.run(
        ["git", "config", "--get", config_key(target)],
        cwd=root,
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.decode().strip()
    if not value:
        return None
    try:
        return validate_instance_name(value)
    except UsageError as exc:
        raise NixantError(
            f"git config {config_key(target)} is invalid: {exc}; "
            f"fix it or run nixant name {target} --unset"
        ) from exc


def _is_multi_worktree(root: Path, runner: Runner) -> bool:
    listing = runner.run(
        ["git", "worktree", "list", "--porcelain"], cwd=root, capture=True
    )
    return listing.stdout.decode().count("worktree ") > 1


def set_override(root: Path, target: str, name: str, runner: Runner) -> str:
    """Write the override and return the scope used ("worktree" or "local")."""
    if not in_git_work_tree(root, runner):
        raise NixantError(
            "outside a git work tree there is nowhere to store the override"
        )
    key = config_key(target)
    if _is_multi_worktree(root, runner):
        runner.run(["git", "config", "extensions.worktreeConfig", "true"], cwd=root)
        runner.run(["git", "config", "--worktree", key, name], cwd=root)
        return "worktree"
    runner.run(["git", "config", key, name], cwd=root)
    return "local"


def unset_override(root: Path, target: str, runner: Runner) -> str | None:
    """Remove the override from whichever scope holds it; None if there was none."""
    if not in_git_work_tree(root, runner):
        raise NixantError(
            "outside a git work tree there is nowhere to store the override"
        )
    key = config_key(target)
    found = runner.run(
        ["git", "config", "--show-scope", "--get", key],
        cwd=root,
        capture=True,
        check=False,
    )
    if found.returncode != 0:
        return None
    scope = found.stdout.decode().split()[0]
    flag = "--worktree" if scope == "worktree" else "--local"
    runner.run(["git", "config", flag, "--unset", key], cwd=root)
    return scope

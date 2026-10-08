"""Per-checkout instance-name overrides, stored in the checkout's git config."""

import re
import sys
from pathlib import Path

from nixant.errors import NixantError, UsageError
from nixant.models import INSTANCE_NAME
from nixant.project import in_git_work_tree, validate_target
from nixant.run import Runner


def validate_instance_name(name: str) -> str:
    if INSTANCE_NAME.fullmatch(name) is None:
        raise UsageError(
            f"invalid instance name {name!r}: use at most 63 characters of "
            "[a-z0-9-], starting with a letter and not ending with a dash"
        )
    return name


def config_key(target: str) -> str:
    validate_target(target)
    return f"nixant.{target}.instanceName"


def sanitize(text: str) -> str:
    """The longest Incus-name-safe form of text; empty when nothing usable remains.

    Callers pick their own fallback: init names a whole instance, propose only
    a suffix.
    """
    base = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return re.sub(r"^[0-9-]+", "", base).strip("-")


def propose(committed: str, checkout: Path) -> str:
    """<committed>-<checkout dir>, trimmed to a valid Incus name."""
    suffix = sanitize(checkout.name) or "checkout"
    head = committed[: max(1, 62 - len(suffix))].rstrip("-")
    return validate_instance_name(f"{head}-{suffix}"[:63].rstrip("-"))


# The override belongs to one checkout; a value in ~/.gitconfig or system
# config would rename the target in every project.
REPO_SCOPES = ("local", "worktree")


def _entries(root: Path, target: str, runner: Runner) -> list[tuple[str, str]]:
    """(scope, value) for every definition of the key, lowest precedence first."""
    result = runner.run(
        ["git", "config", "--show-scope", "--get-all", config_key(target)],
        cwd=root,
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    entries = []
    for line in result.stdout.decode().splitlines():
        scope, _, value = line.partition("\t")
        entries.append((scope, value.strip()))
    return entries


def get_override(root: Path, target: str, runner: Runner) -> str | None:
    """The checkout's override for this target, or None (also outside git)."""
    if not in_git_work_tree(root, runner):
        return None
    entries = _entries(root, target, runner)
    ignored = sorted({scope for scope, _ in entries if scope not in REPO_SCOPES})
    if ignored:
        print(
            f"warning: ignoring {config_key(target)} in {', '.join(ignored)} git "
            "config; instance-name overrides only apply per checkout "
            f"(nixant name {target})",
            file=sys.stderr,
        )
    values = [value for scope, value in entries if scope in REPO_SCOPES]
    value = values[-1] if values else ""
    if not value:
        return None
    try:
        return validate_instance_name(value)
    except UsageError as exc:
        raise NixantError(
            f"git config {config_key(target)} is invalid: {exc}; "
            f"fix it or run nixant name {target} --unset"
        ) from exc


def _uses_worktree_scope(root: Path, runner: Runner) -> bool:
    """Linked worktrees need their own scope; once enabled, it outranks local.

    Removing the linked worktrees leaves the extension and this worktree's
    values in place, so a local write would then be shadowed by them.
    """
    enabled = runner.run(
        ["git", "config", "--type=bool", "--get", "extensions.worktreeConfig"],
        cwd=root,
        capture=True,
        check=False,
    )
    if enabled.returncode == 0 and enabled.stdout.decode().strip() == "true":
        return True
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
    if _uses_worktree_scope(root, runner):
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
    entries = [e for e in _entries(root, target, runner) if e[0] in REPO_SCOPES]
    if not entries:
        return None
    scope = entries[-1][0]
    flag = "--worktree" if scope == "worktree" else "--local"
    runner.run(["git", "config", flag, "--unset-all", config_key(target)], cwd=root)
    return scope

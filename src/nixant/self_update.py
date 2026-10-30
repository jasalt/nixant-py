"""Conservative updates of the current user's Nix profile installation."""

import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from nixant.errors import NixantError
from nixant.run import Runner


def _updatable(url: str) -> bool:
    """Allow known moving sources; never silently replace a revision pin."""
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if any(key in query for key in ("rev", "narHash")):
        return False
    if parsed.scheme in {"github", "gitlab", "sourcehut"}:
        parts = parsed.path.split("/")
        return len(parts) in (2, 3) and not re.fullmatch(
            r"[0-9a-fA-F]{7,64}", parts[-1]
        )
    return parsed.scheme in {"path", "git+file", "git+https", "git+ssh"}


def update(runner: Runner) -> None:
    invocation = Path(shutil.which(sys.argv[0]) or sys.argv[0]).absolute()
    executable = invocation.resolve()
    guidance = (
        f"Executable: {executable}\n"
        "No unambiguous updatable user-profile installation was found. "
        "No changes made. Inspect `nix profile list --json`. "
        "For NixOS/Home Manager, update and rebuild the owning configuration. "
        "For nix run, rerun `nix run --refresh <flake> -- ...`; "
        "for a checkout, update/build that checkout."
    )
    if shutil.which("nix") is None:
        raise NixantError("nix is not on PATH. " + guidance)
    home = Path.home()
    legacy = home / ".nix-profile"
    state = Path(os.environ.get("XDG_STATE_HOME", str(home / ".local/state")))
    profile = legacy if legacy.exists() else state / "nix/profiles/profile"
    # A matching store dependency alone is insufficient: the profile must export
    # this executable, and it must be the command selected on PATH as well.
    exported = profile / "bin/nixant"
    selected = shutil.which("nixant")
    if (
        os.environ.get("IN_NIX_SHELL")
        or not exported.exists()
        or not selected
        or Path(selected).resolve() != exported.resolve()
        or (
            executable != exported.resolve()
            and not (
                executable.name == ".nixant-wrapped"
                and executable.parent == exported.resolve().parent
            )
        )
    ):
        raise NixantError(guidance)
    result = runner.run(
        ["nix", "profile", "list", "--json", "--profile", str(profile)],
        capture=True,
    )
    try:
        data = json.loads(result.stdout)
        elements = data["elements"]
        if not isinstance(elements, dict):
            raise ValueError("unsupported profile format")
        matches = [
            (name, entry) for name, entry in elements.items()
            if isinstance(entry, dict) and entry.get("active", True)
            and any(executable.is_relative_to(Path(p))
                    for p in entry.get("storePaths", []))
        ]
    except (ValueError, KeyError, TypeError) as exc:
        raise NixantError("Cannot interpret Nix profile metadata. " + guidance) from exc
    if len(matches) != 1:
        raise NixantError(guidance)
    name, entry = matches[0]
    url = entry.get("originalUrl", "")
    if not isinstance(url, str) or not _updatable(url):
        raise NixantError(
            f"Profile entry {name!r} has a pinned or unsupported source {url!r}. "
            "Change its source explicitly if desired; no changes made."
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", name):
        raise NixantError("Unsafe profile entry name; " + guidance)
    if url.startswith(("path:", "git+file:")):
        print("Updating from local contents; not running git pull.", file=sys.stderr)
    command = [
        "nix", "profile", "upgrade", name, "--refresh", "--profile", str(profile)
    ]
    print("+ " + shlex.join(command), file=sys.stderr)
    runner.run(command)
    print("Profile update completed. Run nixant version in a new invocation.")

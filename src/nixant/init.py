"""Create a project from a template shipped with this CLI's flake."""

import json
import os
import re
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from nixant.errors import CommandError, NixantError, UsageError
from nixant.project import in_git_work_tree
from nixant.providers.base import Provider
from nixant.run import Runner

NAME_MARKER = "nixant-template-dev"
URL_MARKER = "nixant-template-url"
DEFAULT_TEMPLATE = "default"


def propose_instance_name(directory: Path) -> str:
    """Incus-valid name: lowercase [a-z0-9-], no leading digit or dash, <= 63."""
    base = re.sub(r"[^a-z0-9]+", "-", directory.name.lower())
    base = re.sub(r"^[0-9-]+", "", base)
    base = base[:59].strip("-")
    return f"{base}-dev" if base else "ndev"


def nix_string(value: str) -> str:
    """Contents of a Nix double-quoted string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")


def self_path(environ: Mapping[str, str]) -> Path:
    value = environ.get("NIXANT_SELF", "")
    if not value or not Path(value).is_dir():
        raise NixantError(
            "NIXANT_SELF does not point to the nixant flake; "
            "run nixant from its package or `nix develop` shell"
        )
    return Path(value)


def input_url(environ: Mapping[str, str], flake: Path) -> str:
    """The canonical URL when the package declares one, else this CLI's own source."""
    return environ.get("NIXANT_FLAKE_URL") or f"path:{flake}"


def pin_url(url: str, revision: str) -> str:
    """Pin a flake URL to one revision according to its scheme."""
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in ("github", "gitlab", "sourcehut"):
        # Path form owner/repo[/ref-or-rev]; the revision replaces any ref.
        segments = parts.path.split("/")
        if len(segments) < 2 or not all(segments[:2]):
            raise UsageError(
                f"cannot pin NIXANT_FLAKE_URL {url!r}: expected owner/repo"
            )
        return urlunsplit(
            parts._replace(path="/".join([*segments[:2], revision]), fragment="")
        )
    if scheme.startswith(("git+", "hg+")) or scheme == "git":
        query = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k != "rev"
        ]
        query.append(("rev", revision))
        return urlunsplit(
            parts._replace(
                query=urlencode(query, quote_via=quote, safe="/"), fragment=""
            )
        )
    raise UsageError(
        f"cannot pin NIXANT_FLAKE_URL {url!r}: supported schemes are "
        "github:, gitlab:, sourcehut:, git+*:// and hg+*://"
    )


def list_templates(flake: Path, runner: Runner) -> dict[str, str]:
    result = runner.run(
        [
            "nix",
            "eval",
            "--json",
            f"path:{flake}#templates",
            "--apply",
            'ts: builtins.mapAttrs (_: t: t.description or "") ts',
        ],
        capture=True,
    )
    try:
        data = json.loads(result.stdout)
        return {str(name): str(text) for name, text in data.items()}
    except (ValueError, AttributeError) as exc:
        raise NixantError(f"invalid template listing: {exc}") from exc


def render(text: str, name: str, url: str) -> str:
    return text.replace(NAME_MARKER, nix_string(name)).replace(
        URL_MARKER, nix_string(url)
    )


def _files(directory: Path) -> set[Path]:
    return {
        path.relative_to(directory)
        for path in directory.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(directory).parts
    }


def check_template(templates: Mapping[str, str], template: str) -> None:
    if template not in templates:
        raise UsageError(
            f"unknown template {template!r}; available: "
            + (", ".join(sorted(templates)) or "(none)")
        )


def print_templates(templates: Mapping[str, str]) -> None:
    width = max((len(name) for name in templates), default=0)
    for name, description in sorted(templates.items()):
        print(f"{name.ljust(width)}  {description}")


def init_project(
    directory: Path,
    template: str,
    runner: Runner,
    provider: Provider,
    environ: Mapping[str, str] = os.environ,
) -> None:
    flake = self_path(environ)
    templates = list_templates(flake, runner)
    check_template(templates, template)
    name = propose_instance_name(directory)
    url = input_url(environ, flake)
    if environ.get("NIXANT_FLAKE_URL") and environ.get("NIXANT_REV"):
        pin_url(url, environ["NIXANT_REV"])  # fail before writing anything

    if (directory / "flake.nix").exists():
        snippet = flake / "nix/snippets" / f"{template}.nix"
        if not snippet.is_file():
            raise NixantError(f"template {template!r} has no snippet")
        print("flake.nix already exists; nothing was written.")
        print("Add the following by hand:\n")
        print(render(snippet.read_text(), name, url), end="")
        return

    before = _files(directory)
    runner.run(
        ["nix", "flake", "init", "-t", f"path:{flake}#{template}"], cwd=directory
    )
    created = sorted(_files(directory) - before)
    for relative in created:
        path = directory / relative
        if path.suffix != ".nix":
            continue
        text = path.read_text()
        rendered = render(text, name, url)
        if rendered != text:
            path.write_text(rendered)
    names = [str(path) for path in created]
    print(f"wrote {' '.join(names)} (instanceName: {name})")

    tracked = in_git_work_tree(directory, runner)
    if tracked:
        runner.run(["git", "add", "--", *names], cwd=directory)
    else:
        print(
            "warning: outside a git work tree, Nix will copy the entire directory "
            "into the store on every evaluation; consider git init",
            file=sys.stderr,
        )
    _lock(directory, runner, environ, url)
    if tracked and (directory / "flake.lock").exists():
        runner.run(["git", "add", "--", "flake.lock"], cwd=directory)
        print(f"added to git: {' '.join([*names, 'flake.lock'])}")

    try:
        existing = provider.inspect(name)
    except NixantError:
        existing = None
    if existing is not None:
        print(
            f"warning: an Incus instance named {name} already exists; "
            "edit nixant.instanceName in flake.nix before `nixant up`",
            file=sys.stderr,
        )
    print("next: nixant up")


def _lock(
    directory: Path, runner: Runner, environ: Mapping[str, str], url: str
) -> None:
    revision = environ.get("NIXANT_REV", "")
    command = ["nix", "flake", "lock"]
    pinned = bool(environ.get("NIXANT_FLAKE_URL")) and bool(revision)
    if pinned:
        command += ["--override-input", "nixant", pin_url(url, revision)]
    elif environ.get("NIXANT_FLAKE_URL"):
        print(
            "warning: this build has no revision; the project is pinned to the "
            "latest nixant, not to this CLI",
            file=sys.stderr,
        )
    try:
        runner.run(command, cwd=directory)
    except CommandError:
        print(
            "warning: could not lock inputs; later run: " + shlex.join(command),
            file=sys.stderr,
        )
        return
    if pinned:
        print(f"locked nixant to {revision} (this CLI)")
    else:
        print("locked inputs")

"""The packaged CLI pins a generated project's lock to its own revision.

These build the nixant package with Nix and lock real projects, but never
create Incus instances. A `nix` shim on PATH keeps the network to pinned
inputs: it redirects the canonical nixant URL to this repository at the same
revision and pins nixpkgs to the tool's own lock.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import REPO, git

pytestmark = pytest.mark.integration

CANONICAL = "github:jasalt/nixant-py"


def run(*argv: str, **kwargs: object) -> str:
    return subprocess.run(
        argv, check=True, capture_output=True, text=True, **kwargs
    ).stdout.strip()


def build(installable: str) -> Path:
    return Path(run("nix", "build", "--no-link", "--print-out-paths", installable))


def nixpkgs_rev() -> str:
    lock = json.loads((REPO / "flake.lock").read_text())
    return str(lock["nodes"]["nixpkgs"]["locked"]["rev"])


def nix_shim(directory: Path, head: str) -> Path:
    """A `nix` that logs `flake lock` and redirects it to pinned local inputs."""
    directory.mkdir()
    log = directory / "lock-argv.json"
    real = run("sh", "-c", "command -v nix")
    redirect = {f"{CANONICAL}/{head}": f"git+file://{REPO}?rev={head}"}
    shim = directory / "nix"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['flake', 'lock']:\n"
        f"    open({str(log)!r}, 'w').write(json.dumps(args))\n"
        f"    args = [{redirect!r}.get(a, a) for a in args]\n"
        f"    args += ['--override-input', 'nixpkgs', "
        f"'github:NixOS/nixpkgs/{nixpkgs_rev()}']\n"
        f"os.execv({real!r}, [{real!r}, *args])\n"
    )
    shim.chmod(0o755)
    return log


def init(package: Path, root: Path, shim: Path) -> subprocess.CompletedProcess[str]:
    root.mkdir()
    git(root, "init", "-q")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("NIXANT_")  # the package wrapper must set them
    }
    env["PATH"] = f"{shim}:{env['PATH']}"
    return subprocess.run(
        [str(package / "bin" / "nixant"), "init"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=900,
    )


def lock_nodes(root: Path) -> dict:
    return dict(json.loads((root / "flake.lock").read_text())["nodes"])


def staged(root: Path) -> list[str]:
    return run("git", "diff", "--cached", "--name-only", cwd=root).split()


def test_clean_build_pins_the_project_to_its_revision(tmp_path: Path) -> None:
    head = run("git", "rev-parse", "HEAD", cwd=REPO)
    package = build(f"git+file://{REPO}?rev={head}")
    log = nix_shim(tmp_path / "shim", head)
    result = init(package, tmp_path / "project", tmp_path / "shim")
    root = tmp_path / "project"

    assert f'nixant.url = "{CANONICAL}";' in (root / "flake.nix").read_text()
    argv = json.loads(log.read_text())
    assert argv[argv.index("--override-input") + 1 :][:2] == [
        "nixant",
        f"{CANONICAL}/{head}",
    ]
    assert f"locked nixant to {head}" in result.stdout
    nodes = lock_nodes(root)
    assert nodes["nixant"]["locked"]["rev"] == head
    assert nodes["nixant"]["inputs"]["nixpkgs"] == ["nixpkgs"]
    assert nodes["nixpkgs"]["locked"]["rev"] == nixpkgs_rev()
    assert "flake.lock" in staged(root)


def test_dirty_build_points_the_project_at_its_own_source(tmp_path: Path) -> None:
    head = run("git", "rev-parse", "HEAD", cwd=REPO)
    source = tmp_path / "source"  # a copy without .git has no revision
    source.mkdir()
    subprocess.run(
        f"git -C {REPO} archive HEAD | tar -x -C {source}", shell=True, check=True
    )
    package = build(f"path:{source}")
    log = nix_shim(tmp_path / "shim", head)
    result = init(package, tmp_path / "project", tmp_path / "shim")
    root = tmp_path / "project"

    wrapper = (package / "bin" / "nixant").read_text()
    found = re.search(r"NIXANT_SELF='(/nix/store/[^']+)'", wrapper)
    assert found is not None, wrapper
    self_store = found[1]
    assert f'nixant.url = "path:{self_store}";' in (root / "flake.nix").read_text()
    assert json.loads(log.read_text()) == ["flake", "lock"]
    assert "locked inputs" in result.stdout
    nodes = lock_nodes(root)
    assert nodes["nixant"]["locked"]["type"] == "path"
    assert nodes["nixant"]["locked"]["path"] == self_store
    assert "rev" not in nodes["nixant"]["locked"]
    assert nodes["nixpkgs"]["locked"]["rev"] == nixpkgs_rev()
    assert "flake.lock" in staged(root)

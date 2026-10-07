"""Scenarios run against real Incus; enable with NIXANT_INTEGRATION=1."""

import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if os.environ.get("NIXANT_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="set NIXANT_INTEGRATION=1 to run against Incus")
    for item in items:
        if "integration" in item.nodeid:
            item.add_marker(skip)


@dataclass
class Project:
    root: Path
    instance: str
    created: list[str] = field(default_factory=list)

    def nixant(
        self, *args: str, check: bool = True, timeout: float = 1800
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PYTHONPATH": str(REPO / "src"),
            "NIXANT_SELF": os.environ.get("NIXANT_SELF", str(REPO)),
        }
        result = subprocess.run(
            [sys.executable, "-m", "nixant", *args],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"nixant {' '.join(args)} exited {result.returncode}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    def exec(
        self, *command: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.nixant("exec", "--", *command, check=check)

    def write_module(self, text: str, name: str = "extra") -> None:
        """Add a NixOS module to the target; Nix only sees tracked files."""
        path = self.root / "nix" / f"{name}.nix"
        path.write_text(text)
        flake = (self.root / "flake.nix").read_text()
        reference = f"./nix/{name}.nix"
        if reference not in flake:
            flake = flake.replace("./nix/dev.nix", f"./nix/dev.nix {reference}", 1)
            (self.root / "flake.nix").write_text(flake)
        git(self.root, "add", "--", "flake.nix", f"nix/{name}.nix")

    def incus_config(self, key: str, instance: str | None = None) -> str:
        return incus(
            "config", "get", f"local:{instance or self.instance}", key
        ).stdout.strip()

    def instance_exists(self, instance: str | None = None) -> bool:
        result = incus("info", f"local:{instance or self.instance}", check=False)
        return result.returncode == 0


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def incus(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["incus", *args], capture_output=True, text=True, check=check)


def new_project(tmp_path: Path, label: str) -> Project:
    root = tmp_path / label
    root.mkdir()
    git(root, "init", "-q")
    instance = f"nixit-{secrets.token_hex(3)}"
    project = Project(root, instance)
    project.nixant("init")
    flake = (root / "flake.nix").read_text()
    flake = re.sub(
        r'nixant\.instanceName = "[^"]*";',
        f'nixant.instanceName = "{instance}";',
        flake,
    )
    pinned = os.environ.get("NIXANT_IT_NIXPKGS")
    if pinned:  # e.g. github:NixOS/nixpkgs/nixos-26.05 to check the supported minimum
        flake = re.sub(r'nixpkgs\.url = "[^"]*";', f'nixpkgs.url = "{pinned}";', flake)
    (root / "flake.nix").write_text(flake)
    project.write_module(f"{{ nixant.user.uid = {os.getuid()}; }}\n", name="extra")
    return project


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Project]:
    project = new_project(tmp_path, "project")
    try:
        yield project
    finally:
        cleanup(project)


def cleanup(project: Project) -> None:
    for name in {project.instance, *project.created}:
        incus("delete", "--force", f"local:{name}", check=False)


def instance_created_at(name: str) -> str:
    data = json.loads(incus("query", f"/1.0/instances/{name}").stdout)
    return str(data["created_at"])

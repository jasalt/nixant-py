import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import Project, cleanup, instance_created_at, new_project

pytestmark = pytest.mark.integration


def test_happy_path(project: Project) -> None:
    up = project.nixant("up")
    assert f"{project.instance} ready" in up.stdout
    assert project.exec("hostname").stdout.strip() == project.instance
    project.exec("touch", "/workspace/x")
    assert (project.root / "x").stat().st_uid == os.getuid()
    project.exec("sudo", "true")
    assert "wheel" in project.exec("id", "-Gn").stdout.split()
    assert project.incus_config("user.nixant.activation") == "ok"

    created = instance_created_at(project.instance)
    project.write_module(
        f"{{ nixant.user.uid = {os.getuid()}; "
        'environment.etc."nixant-marker".text = "v2"; }\n'
    )
    project.nixant("rebuild")
    assert project.exec("cat", "/etc/nixant-marker").stdout == "v2"
    assert instance_created_at(project.instance) == created

    project.nixant("down")
    assert "STOPPED" in project.nixant("status").stdout.upper()
    project.nixant("up")
    status = project.nixant("status").stdout
    assert "RUNNING" in status.upper()
    assert "current" in status

    project.nixant("destroy")
    assert not project.instance_exists()


def test_stale_etc_nixos_guard(project: Project) -> None:
    project.nixant("up")
    stub = project.exec("cat", "/etc/nixos/configuration.nix").stdout
    assert "managed by nixant" in stub
    assert (
        project.exec("test", "-e", "/etc/nixos/incus.nix", check=False).returncode != 0
    )
    switch = project.exec("sudo", "nixos-rebuild", "switch", check=False)
    assert switch.returncode != 0


def test_guest_side_nix_builds(project: Project) -> None:
    project.nixant("up")
    bash = project.exec(
        "readlink", "-f", "/run/current-system/sw/bin/bash"
    ).stdout.strip()
    expression = (
        'derivation { name = "nixant-nesting"; system = builtins.currentSystem; '
        f'builder = builtins.storePath "{bash}"; '
        'args = [ "-c" "echo ok > $out" ]; }'
    )
    built = project.exec(
        "nix",
        "build",
        "--impure",
        "--no-link",
        "--print-out-paths",
        "--expr",
        expression,
    )
    assert built.stdout.strip().startswith("/nix/store/")


def test_shell_independence_with_fish(project: Project) -> None:
    project.write_module(
        f"{{ pkgs, ... }}: {{ nixant.user.uid = {os.getuid()}; "
        "programs.fish.enable = true; users.users.dev.shell = pkgs.fish; "
        'environment.etc."nixant-marker".text = "fish"; }\n'
    )
    project.nixant("up")
    shell = project.exec("sh", "-c", "getent passwd dev | cut -d: -f7").stdout.strip()
    assert shell.endswith("/fish")
    assert project.exec("cat", "/etc/nixant-marker").stdout == "fish"
    assert project.exec("bash", "-c", "echo $HOME").stdout.strip() == "/home/dev"


def test_degraded_service_recovery(project: Project) -> None:
    uid = os.getuid()
    failing = (
        f"{{ pkgs, ... }}: {{ nixant.user.uid = {uid}; "
        'systemd.services.nixant-fail = { wantedBy = [ "multi-user.target" ]; '
        'serviceConfig.ExecStart = "${pkgs.coreutils}/bin/false"; }; }\n'
    )
    project.write_module(failing)
    up = project.nixant("up")
    assert "failed units" in up.stderr
    assert project.incus_config("user.nixant.activation") == "degraded"
    assert "degraded" in project.nixant("status").stdout

    # A second up must re-activate rather than treating degraded as current.
    again = project.nixant("up")
    assert "failed units" in again.stderr

    project.write_module(f"{{ nixant.user.uid = {uid}; }}\n")
    project.nixant("up")
    assert project.incus_config("user.nixant.activation") == "ok"


def test_interrupted_import_recovers(project: Project) -> None:
    project.nixant("up")
    project.write_module(
        f"{{ pkgs, ... }}: {{ nixant.user.uid = {os.getuid()}; "
        "environment.systemPackages = [ pkgs.hello pkgs.cowsay pkgs.figlet ]; }\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
        "NIXANT_SELF": os.environ.get("NIXANT_SELF", str(Path(__file__).parents[2])),
    }
    process = subprocess.Popen(
        ["python3", "-m", "nixant", "rebuild"],
        cwd=project.root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline and process.poll() is None:
        if project.incus_config("user.nixant.activation") == "pending":
            process.send_signal(signal.SIGINT)
            break
        time.sleep(0.2)
    process.wait(timeout=120)
    # Whether interrupted or already finished, a fresh up leaves a good system.
    project.nixant("rebuild")
    assert project.incus_config("user.nixant.activation") == "ok"
    assert "figlet" in project.exec("sh", "-c", "ls /run/current-system/sw/bin").stdout


def test_syntax_error_creates_nothing(project: Project) -> None:
    (project.root / "nix/dev.nix").write_text("{ this is not nix\n")
    subprocess.run(["git", "add", "nix/dev.nix"], cwd=project.root, check=True)
    result = project.nixant("up", check=False)
    assert result.returncode != 0
    assert not project.instance_exists()
    # Cleanup commands never evaluate, so a broken configuration cannot block them.
    assert project.nixant("destroy", check=False).returncode == 0


def test_cross_target_same_instance_name(tmp_path: Path) -> None:
    project = new_project(tmp_path, "cross")
    try:
        flake = project.root / "flake.nix"
        flake.write_text(with_second_target(flake.read_text()))
        subprocess.run(["git", "add", "flake.nix"], cwd=project.root, check=True)
        project.nixant("up", "dev")
        clash = project.nixant("up", "other", check=False)
        assert clash.returncode != 0
        assert "belongs to target dev" in clash.stderr
        # The second target never touches the first target's instance.
        project.nixant("destroy", "other")
        assert project.instance_exists()
        assert project.incus_config("user.nixant.target") == "dev"
    finally:
        cleanup(project)


def with_second_target(text: str) -> str:
    """Duplicate the template's target as `other`, reusing the same instanceName."""
    start = text.index("nixosConfigurations.dev = ")
    end = text.index("    };\n", start) + len("    };\n")
    block = text[start:end].replace(
        "nixosConfigurations.dev", "nixosConfigurations.other"
    )
    return text[:end] + "    " + block + text[end:]

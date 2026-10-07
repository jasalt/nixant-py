import json
import os
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from .conftest import Project, cleanup, incus, instance_created_at, new_project

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

    project.nixant("restart")
    assert project.exec("hostname").stdout.strip() == project.instance
    assert project.exec("cat", "/etc/nixant-marker").stdout == "v2"

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


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_machine_settings_reconcile(project: Project, tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "hello").write_text("hi")
    port = free_port()

    def settings(cpus: int, disk: str, ports: bool = True, mount: bool = True) -> str:
        return (
            f"{{ pkgs, ... }}: {{ nixant.user.uid = {os.getuid()}; "
            f"environment.systemPackages = [ pkgs.python3 ]; nixant.cpus = {cpus}; "
            f'nixant.memory = "1GiB"; nixant.disk = "{disk}"; '
            + (
                f"nixant.ports = [ {{ host = {port}; guest = 8080; }} ]; "
                if ports
                else ""
            )
            + (
                f'nixant.mounts.data = {{ source = "{data}"; target = "/data"; '
                "readOnly = true; }; "
                if mount
                else ""
            )
            + "}\n"
        )

    project.write_module(settings(2, "20GiB"))
    project.nixant("up")
    assert project.incus_config("limits.cpu") == "2"
    assert project.exec("nproc").stdout.strip() == "2"
    assert int(project.incus_config("limits.memory").removesuffix("B")) == 2**30
    root = incus("config", "device", "get", f"local:{project.instance}", "root", "size")
    assert (
        int(root.stdout.strip().removesuffix("B")) == 20 * 2**30 or "20" in root.stdout
    )
    assert project.exec("cat", "/data/hello").stdout == "hi"
    assert project.exec("touch", "/data/x", check=False).returncode != 0

    # The forwarded port reaches a guest service.
    project.exec("sh", "-c", "nohup python3 -m http.server 8080 >/dev/null 2>&1 &")
    deadline = time.monotonic() + 30
    body = ""
    while time.monotonic() < deadline and "Directory listing" not in body:
        try:
            body = (
                urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=2)
                .read()
                .decode()
            )
        except OSError:
            time.sleep(0.5)
    assert "Directory listing" in body

    # Live changes: grow the disk, change limits, drop the port and the mount.
    project.write_module(settings(1, "24GiB", ports=False, mount=False))
    project.nixant("up")
    assert project.incus_config("limits.cpu") == "1"
    assert project.exec("nproc").stdout.strip() == "1"
    assert project.exec("test", "-e", "/data/hello", check=False).returncode != 0
    devices = incus("config", "device", "list", f"local:{project.instance}").stdout
    assert "nixant-port" not in devices and "nixant-mount-data" not in devices

    # Shrinking is refused and leaves everything as it was.
    project.write_module(settings(1, "10GiB", ports=False, mount=False))
    refused = project.nixant("up", check=False)
    assert refused.returncode != 0
    assert "cannot shrink" in refused.stderr + refused.stdout
    assert project.incus_config("limits.cpu") == "1"


def test_adopt_after_checkout_moves(project: Project, tmp_path: Path) -> None:
    project.nixant("up")
    (project.root / "marker").write_text("kept")
    moved = tmp_path / "moved"
    project.root.rename(moved)
    new = replace(project, root=moved)
    assert project.instance in new.nixant("status", "--orphans").stdout
    # The ownership check refuses the unrelated checkout until it is adopted.
    assert new.nixant("exec", "--", "true", check=False).returncode != 0
    new.nixant("adopt")
    assert new.exec("cat", "/workspace/marker").stdout == "kept"
    assert project.instance not in new.nixant("status", "--orphans").stdout
    new.nixant("rebuild")
    assert new.incus_config("user.nixant.root") == str(moved.resolve())
    new.nixant("destroy")


def test_name_override_gives_second_checkout_its_own_instance(
    project: Project, tmp_path: Path
) -> None:
    project.nixant("up")
    subprocess.run(["git", "add", "-A"], cwd=project.root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
        cwd=project.root,
        check=True,
    )
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(project.root), clone], check=True)
    second = replace(project, root=clone)
    clash = second.nixant("up", check=False)
    assert clash.returncode != 0
    assert "nixant name dev" in clash.stderr + clash.stdout
    chosen = f"{project.instance}-b"
    second.created[:] = [chosen]
    second.nixant("name", "dev", chosen)
    second.nixant("up")
    assert second.instance_exists(chosen)
    assert project.instance_exists()
    second.nixant("destroy")
    assert not second.instance_exists(chosen)
    assert project.instance_exists()


def test_vm_lifecycle_and_settings(vm_project: Project) -> None:
    project = vm_project
    project.nixant("up")
    assert project.incus_config("user.nixant.activation") == "ok"
    info = json.loads(incus("query", f"/1.0/instances/{project.instance}").stdout)
    assert info["type"] == "virtual-machine"
    assert project.exec("hostname").stdout.strip() == project.instance
    project.exec("sudo", "true")
    assert "wheel" in project.exec("id", "-Gn").stdout.split()
    project.exec("touch", "/workspace/from-vm")
    assert (project.root / "from-vm").exists()

    # CPU and memory apply live; disk growth waits for a restart.
    project.write_module(
        f"{{ nixant.user.uid = {os.getuid()}; nixant.cpus = 2; "
        'nixant.memory = "3GiB"; nixant.disk = "24GiB"; }\n'
    )
    changed = project.nixant("up")
    assert "takes effect after the next restart" in changed.stderr
    assert project.exec("nproc").stdout.strip() == "2"
    project.nixant("restart")
    size = project.exec("df", "--output=size", "-BG", "/").stdout.split()[-1]
    assert int(size.removesuffix("G")) >= 20

    # Ports cannot be forwarded to a VM; the error leaves the instance alone.
    project.write_module(
        f"{{ nixant.user.uid = {os.getuid()}; "
        "nixant.ports = [ { host = 18080; guest = 80; } ]; }\n"
    )
    refused = project.nixant("up", check=False)
    assert refused.returncode != 0
    assert "NAT" in refused.stderr + refused.stdout

    project.nixant("down")
    project.nixant("destroy")
    assert not project.instance_exists()

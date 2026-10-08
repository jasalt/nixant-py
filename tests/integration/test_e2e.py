import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from .conftest import (
    Project,
    cleanup,
    commit_all,
    git,
    incus,
    instance_created_at,
    new_project,
    nixant_argv,
    nixant_env,
)

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
    # exec hands the caller's stdin to the guest command.
    assert project.nixant("exec", "--", "cat", input="piped\n").stdout == "piped\n"

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


def test_guest_flake_build_and_develop_in_workspace(project: Project) -> None:
    project.nixant("up")
    guest = project.root / "guest"
    guest.mkdir()
    # The guest registry resolves nixpkgs to the system's own source, and
    # bash is already in the guest store, so nothing needs the network.
    (guest / "flake.nix").write_text(
        """{
  inputs.nixpkgs.url = "nixpkgs";
  outputs = { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in {
      packages.${system}.default = derivation {
        name = "nixant-workspace";
        inherit system;
        builder = "${pkgs.bash}/bin/bash";
        args = [ "-c" "echo built-in-guest > $out" ];
      };
      devShells.${system}.default = pkgs.mkShellNoCC {
        NIXANT_MARKER = "from-workspace";
      };
    };
}
"""
    )
    git(project.root, "add", "guest/flake.nix")
    built = project.exec(
        "nix", "build", "--no-link", "--print-out-paths", "/workspace/guest"
    )
    output = built.stdout.strip()
    assert project.exec("cat", output).stdout.strip() == "built-in-guest"
    assert (guest / "flake.lock").is_file()  # written through the mount
    developed = project.exec(
        "nix", "develop", "/workspace/guest", "-c", "sh", "-c", "echo $NIXANT_MARKER"
    )
    assert developed.stdout.strip() == "from-workspace"


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
    # `shell` itself starts fish as a login shell in the workdir; fish reads
    # the script from stdin because no terminal is attached.
    entered = project.nixant(
        "shell",
        input='echo "fish=$FISH_VERSION"\nstatus is-login; and echo login\npwd\n',
    )
    lines = entered.stdout.split()
    assert lines[0].startswith("fish=") and lines[0] != "fish="
    assert lines[1:] == ["login", "/workspace"]


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
    before = project.incus_config("user.nixant.system")
    # A large, guest-new path keeps the transfer running long enough to
    # interrupt it while the import is demonstrably in progress.
    project.write_module(
        f"{{ pkgs, ... }}: {{ nixant.user.uid = {os.getuid()}; "
        'environment.etc."nixant-payload".source = pkgs.runCommand '
        '"nixant-payload" {} "head -c 256M /dev/urandom > $out"; }\n'
    )
    log = project.root.parent / "interrupted-up.log"
    with log.open("w") as stderr:
        process = subprocess.Popen(
            nixant_argv("up"),
            cwd=project.root,
            env=nixant_env(),
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
    importing = f"local:{project.instance} -- nix-store --import"
    deadline = time.monotonic() + 900
    try:
        while not running(importing):
            assert process.poll() is None, log.read_text()
            assert time.monotonic() < deadline, "the import never started"
            time.sleep(0.05)
        process.send_signal(signal.SIGINT)
        process.wait(timeout=120)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode != 0
    assert "activation interrupted; run nixant up to retry" in log.read_text()
    settle = time.monotonic() + 10
    while running(importing) and time.monotonic() < settle:
        time.sleep(0.2)
    assert not running(importing)
    assert project.incus_config("user.nixant.activation") == "pending"
    assert project.incus_config("user.nixant.system") == before
    assert "(pending," in project.nixant("status").stdout
    payload = "/etc/nixant-payload"
    assert project.exec("test", "-e", payload, check=False).returncode != 0

    # A plain up retries: pending never counts as the current system.
    project.nixant("up")
    assert project.incus_config("user.nixant.activation") == "ok"
    assert project.incus_config("user.nixant.system") != before
    assert project.exec("stat", "-L", "-c", "%s", payload).stdout.strip() == str(
        256 * 2**20
    )


def running(pattern: str) -> bool:
    return (
        subprocess.run(["pgrep", "-f", "--", pattern], capture_output=True).returncode
        == 0
    )


def test_broken_config_does_not_block_cleanup(project: Project) -> None:
    project.nixant("up")
    (project.root / "nix/dev.nix").write_text("{ this is not nix\n")
    git(project.root, "add", "nix/dev.nix")
    assert project.nixant("up", check=False).returncode != 0
    assert project.incus_config("user.nixant.activation") == "ok"
    # status, down and destroy never evaluate the configuration.
    assert "RUNNING" in project.nixant("status").stdout.upper()
    project.nixant("down")
    assert "STOPPED" in project.nixant("status").stdout.upper()
    project.nixant("destroy")
    assert not project.instance_exists()


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


def test_cross_target_distinct_names_coexist(tmp_path: Path) -> None:
    project = new_project(tmp_path, "pair")
    other = f"{project.instance}-o"
    project.created.append(other)
    try:
        flake = project.root / "flake.nix"
        flake.write_text(with_second_target(flake.read_text(), other))
        git(project.root, "add", "flake.nix")
        project.nixant("up", "dev")
        project.nixant("up", "other")
        assert project.instance_exists() and project.instance_exists(other)
        status = project.nixant("status").stdout
        assert "target: dev" in status and "target: other" in status
        assert project.exec("hostname").stdout.strip() == project.instance
        hostname = project.nixant("exec", "-n", "other", "--", "hostname")
        assert hostname.stdout.strip() == other
        assert project.incus_config("user.nixant.target", other) == "other"

        project.nixant("down", "other")
        assert "RUNNING" in project.nixant("status", "dev").stdout.upper()
        project.nixant("destroy", "other")
        assert not project.instance_exists(other)
        assert project.exec("true").returncode == 0
    finally:
        cleanup(project)


def with_second_target(text: str, instance: str | None = None) -> str:
    """Duplicate the template's target as `other`, optionally renaming it."""
    start = text.index("nixosConfigurations.dev = ")
    end = text.index("    };\n", start) + len("    };\n")
    block = text[start:end].replace(
        "nixosConfigurations.dev", "nixosConfigurations.other"
    )
    if instance is not None:
        block = re.sub(
            r'nixant\.instanceName = "[^"]*";',
            f'nixant.instanceName = "{instance}";',
            block,
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


def test_linked_worktree_lifecycle_and_orphans(
    project: Project, tmp_path: Path
) -> None:
    project.nixant("up")
    commit_all(project.root)
    linked = tmp_path / "linked"
    git(project.root, "worktree", "add", "-q", str(linked), "-b", "feature")
    second = replace(project, root=linked)
    clash = second.nixant("up", check=False)
    assert clash.returncode != 0
    assert "nixant name dev" in clash.stderr + clash.stdout

    chosen = f"{project.instance}-w"
    project.created.append(chosen)
    named = second.nixant("name", "dev", chosen)
    assert "worktree scope" in named.stdout
    second.nixant("up")
    assert second.exec("hostname").stdout.strip() == project.instance
    assert second.incus_config("user.nixant.root", chosen) == str(linked.resolve())
    assert f"{chosen} (override)" in second.nixant("status").stdout
    # The main worktree keeps its committed name and its own instance.
    assert "(override)" not in project.nixant("status").stdout
    assert project.exec("true").returncode == 0

    second.nixant("down")
    second.nixant("up")
    assert second.exec("true").returncode == 0

    git(project.root, "worktree", "remove", "--force", str(linked))
    orphans = project.nixant("status", "--orphans").stdout
    assert chosen in orphans and str(linked) in orphans
    assert f"{project.instance} " not in orphans
    incus("delete", "--force", f"local:{chosen}")
    assert chosen not in project.nixant("status", "--orphans").stdout


def test_vm_mount_hotplug_and_adopt(tmp_path: Path) -> None:
    project = new_project(tmp_path, "vmmounts", vm=True)
    try:
        project.nixant("up")
        boot = project.exec("cat", "/proc/sys/kernel/random/boot_id").stdout
        data = tmp_path / "data"
        data.mkdir()
        (data / "hello").write_text("hi")
        project.write_module(
            f"{{ nixant.user.uid = {os.getuid()}; "
            f'nixant.mounts.data = {{ source = "{data}"; target = "/data"; }}; }}\n'
        )
        project.nixant("up")
        assert project.exec("cat", "/data/hello").stdout == "hi"

        (project.root / "marker").write_text("kept")
        moved = tmp_path / "vmmounts-moved"
        project.root.rename(moved)
        new = replace(project, root=moved)
        new.nixant("adopt")
        assert new.exec("cat", "/workspace/marker").stdout == "kept"
        new.exec("touch", "/workspace/from-vm")
        assert (moved / "from-vm").exists()
        assert new.exec("cat", "/data/hello").stdout == "hi"
        # Hot-plug and the adopted retarget never rebooted the VM.
        assert new.exec("cat", "/proc/sys/kernel/random/boot_id").stdout == boot
        new.nixant("destroy")
    finally:
        cleanup(project)


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


def test_snapshot_and_restore(project: Project) -> None:
    project.nixant("up")
    project.exec("sh", "-c", "echo before > ~/state")
    assert "created snapshot safe" in project.nixant("snapshot", "safe").stdout
    assert "safe" in project.nixant("snapshots").stdout
    assert project.nixant("snapshot", "safe", check=False).returncode != 0

    project.exec("sh", "-c", "echo after > ~/state; touch ~/extra")
    project.nixant("restore", "safe")
    assert project.exec("cat", "/home/dev/state").stdout.strip() == "before"
    assert project.exec("test", "-e", "/home/dev/extra", check=False).returncode != 0
    # Ownership metadata survives the rollback, so normal commands still work.
    assert project.incus_config("user.nixant.activation") == "ok"
    project.nixant("rebuild")

    project.nixant("snapshot", "safe", "--delete")
    assert "safe" not in project.nixant("snapshots").stdout
    assert project.nixant("restore", "safe", check=False).returncode != 0


def test_ephemeral_instance_vanishes_on_down(project: Project) -> None:
    uid = os.getuid()
    project.write_module(f"{{ nixant.user.uid = {uid}; nixant.ephemeral = true; }}\n")
    project.nixant("up")
    info = json.loads(incus("query", f"/1.0/instances/{project.instance}").stdout)
    assert info["ephemeral"] is True
    assert "ephemeral" in project.nixant("status").stdout
    project.nixant("snapshot", "s1")
    down = project.nixant("down")
    assert "deleted (ephemeral)" in down.stdout
    assert not project.instance_exists()
    project.nixant("up")
    assert project.exec("hostname").stdout.strip() == project.instance

    # The setting is fixed at creation; flipping it needs a new instance.
    project.write_module(f"{{ nixant.user.uid = {uid}; }}\n")
    refused = project.nixant("up", check=False)
    assert refused.returncode != 0
    assert "ephemeral" in refused.stderr + refused.stdout


def test_agent_isolation_profile(project: Project, tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "doc").write_text("read me")
    project.write_module(
        f'{{ nixant.user.uid = {os.getuid()}; nixant.isolation = "agent"; '
        f'nixant.mounts.reference = {{ source = "{reference}"; '
        'target = "/reference"; readOnly = true; }; }\n'
    )
    project.nixant("up")
    assert project.exec("sudo", "-n", "true", check=False).returncode != 0
    assert "wheel" not in project.exec("id", "-Gn").stdout.split()
    assert project.exec("nproc").stdout.strip() == "2"
    project.exec("touch", "/workspace/agent-output")
    assert (project.root / "agent-output").exists()
    assert project.exec("cat", "/reference/doc").stdout == "read me"
    assert project.exec("touch", "/reference/x", check=False).returncode != 0
    trusted = project.exec("nix", "config", "show", "trusted-users").stdout.split()
    assert "dev" not in trusted

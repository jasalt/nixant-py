import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nixant.errors import NixantError, UsageError
from nixant.project import (
    discover_project,
    evaluation_preflight,
    project_id,
    resolve_mount_sources,
    state_directory,
    target_lock,
    validate_target,
)
from nixant.run import Runner


@pytest.fixture
def git_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Isolate from host ignore/config settings and inherited Git overrides.
    for key in os.environ:
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    Runner().run(["git", "init", "-q", str(tmp_path)])
    return tmp_path


def test_discovery_nearest_and_symlink(tmp_path: Path) -> None:
    (tmp_path / "flake.nix").touch()
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "flake.nix").touch()
    child = nested / "a/b"
    child.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(child, target_is_directory=True)
    assert discover_project(alias) == nested
    assert project_id(alias) == project_id(child)
    assert project_id(nested) == hashlib.sha256(os.fsencode(nested)).hexdigest()[:12]
    assert project_id(nested) != project_id(tmp_path)


def test_discovery_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "flake.nix").touch()
    monkeypatch.chdir(tmp_path)
    assert discover_project() == tmp_path


def test_missing_flake(tmp_path: Path) -> None:
    (tmp_path / "flake.nix").mkdir()  # A directory is not a flake.
    with pytest.raises(NixantError, match="no flake.nix found"):
        discover_project(tmp_path)


@pytest.mark.parametrize("target", ["dev", "_test", "Test-1", "x_2"])
def test_valid_target(target: str) -> None:
    assert validate_target(target) == target


@pytest.mark.parametrize(
    "target", ["", "1dev", "-dev", "a.b", "a/b", "a'", "dev\n", "é"]
)
def test_invalid_target(target: str) -> None:
    with pytest.raises(UsageError, match="invalid target"):
        validate_target(target)


def test_mount_sources(tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.touch()
    alias = tmp_path / "alias"
    alias.symlink_to(file)
    assert resolve_mount_sources(
        tmp_path, {"workspace": ".", "file": str(file), "alias": "alias"}
    ) == {"workspace": tmp_path, "file": file, "alias": file}
    assert resolve_mount_sources(tmp_path, {}) == {}
    with pytest.raises(NixantError, match=r"nixant.mounts.bad.source.*missing"):
        resolve_mount_sources(tmp_path, {"bad": "missing"})
    alias.unlink()
    alias.symlink_to(tmp_path / "missing")
    with pytest.raises(NixantError, match="does not exist"):
        resolve_mount_sources(tmp_path, {"broken": "alias"})


def test_preflight_untracked(
    git_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (git_root / "flake.nix").touch()
    (git_root / "flake.lock").touch()
    (git_root / "notes.txt").touch()
    (git_root / "ignored.nix").touch()
    (git_root / ".gitignore").write_text("ignored.nix\n")
    (git_root / "nix").mkdir()
    (git_root / "nix/dev config.nix").touch()
    Runner().run(["git", "add", "flake.nix"], cwd=git_root)
    evaluation_preflight(git_root, Runner())
    warning = capsys.readouterr().err
    assert "Nix ignores files not tracked by git" in warning
    assert "flake.lock" in warning
    assert "'nix/dev config.nix'" in warning
    assert "run: git add --" in warning
    assert "flake.nix" not in warning
    assert "notes.txt" not in warning
    assert "ignored.nix" not in warning
    Runner().run(["git", "add", "flake.lock", "nix"], cwd=git_root)
    evaluation_preflight(git_root, Runner())
    assert capsys.readouterr().err == ""


def test_preflight_from_nested_project(
    git_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (git_root / "outer.nix").touch()
    nested = git_root / "nested"
    nested.mkdir()
    (nested / "flake.nix").touch()
    evaluation_preflight(nested, Runner())
    warning = capsys.readouterr().err
    assert "flake.nix" in warning
    assert "outer.nix" not in warning


def test_non_git_preflight(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    evaluation_preflight(tmp_path, Runner())
    warning = capsys.readouterr().err
    assert warning.count("warning:") == 1
    assert "entire directory" in warning
    assert "git init" in warning


@pytest.mark.parametrize("xdg", ["", "relative/path", "/tmp/nixant-test-state"])
def test_state_directory(
    xdg: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", xdg)
    expected = Path(xdg) if xdg.startswith("/") else tmp_path / ".local/state"
    assert state_directory() == expected / "nixant"


def test_lock_contention_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    with target_lock(tmp_path, "dev"):
        with pytest.raises(NixantError, match="another nixant command.*dev"):
            with target_lock(tmp_path, "dev"):
                pytest.fail("contended lock acquired")
        with target_lock(tmp_path, "other"):
            pass
        with target_lock(tmp_path / "other", "dev"):
            pass
        # Verify the advisory lock also excludes a distinct process.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; from nixant.project import target_lock; "
                f"\nwith target_lock(Path({str(tmp_path)!r}), 'dev'): pass",
            ],
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 1
        assert b"another nixant command" in result.stderr
    lockfile = state_directory() / "locks" / f"{project_id(tmp_path)}-dev.lock"
    inode = lockfile.stat().st_ino
    with pytest.raises(RuntimeError), target_lock(tmp_path, "dev"):
        raise RuntimeError("failed operation")
    with target_lock(tmp_path, "dev"):
        assert lockfile.stat().st_ino == inode


def test_lock_rejects_invalid_target(tmp_path: Path) -> None:
    with pytest.raises(UsageError), target_lock(tmp_path, "../escape"):
        pytest.fail("invalid target accepted")

import hashlib
import os
from pathlib import Path

import pytest

from nixant.errors import NixantError, UsageError
from nixant.project import (
    config_directory,
    discover_project,
    flake_reference,
    project_id,
    resolve_mount_sources,
    state_directory,
    target_lock,
    validate_target,
)


def project(root: Path) -> None:
    config_directory(root).mkdir(parents=True)
    (config_directory(root) / "flake.nix").touch()


def test_discovery_nearest_and_symlink(tmp_path: Path) -> None:
    project(tmp_path)
    nested = tmp_path / "nested"
    project(nested)
    child = nested / "a/b"
    child.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(child, target_is_directory=True)
    assert discover_project(alias) == nested
    assert discover_project(config_directory(nested)) == nested
    assert project_id(alias) == project_id(child)
    assert project_id(nested) == hashlib.sha256(os.fsencode(nested)).hexdigest()[:12]
    assert project_id(nested) != project_id(tmp_path)


def test_unrelated_nested_flake_does_not_hide_workspace(tmp_path: Path) -> None:
    project(tmp_path)
    nested = tmp_path / "repository"
    nested.mkdir()
    (nested / "flake.nix").touch()
    assert discover_project(nested) == tmp_path


def test_discovery_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert discover_project() == tmp_path


def test_missing_flake(tmp_path: Path) -> None:
    with pytest.raises(NixantError, match="no .nixant/flake.nix found"):
        discover_project(tmp_path)


def test_legacy_flake_needs_migration(tmp_path: Path) -> None:
    (tmp_path / "flake.nix").touch()
    with pytest.raises(NixantError, match="migrate"):
        discover_project(tmp_path)


def test_source_is_only_configuration(tmp_path: Path) -> None:
    root = tmp_path / "workspace #1"
    project(root)
    assert (
        flake_reference(root)
        == "path:" + str(root).replace(" #", "%20%23") + "/.nixant"
    )
    assert resolve_mount_sources(root, {"workspace": "."}) == {"workspace": root}


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
    with pytest.raises(NixantError, match="does not exist"):
        resolve_mount_sources(tmp_path, {"bad": "missing"})


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
    with pytest.raises(RuntimeError), target_lock(tmp_path, "dev"):
        raise RuntimeError("failed operation")
    with target_lock(tmp_path, "dev"):
        pass


def test_lock_rejects_invalid_target(tmp_path: Path) -> None:
    with pytest.raises(UsageError), target_lock(tmp_path, "../escape"):
        pytest.fail("invalid target accepted")

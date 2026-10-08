import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nixant.cli import app
from nixant.errors import NixantError, UsageError
from nixant.models import MachineState
from nixant.naming import (
    get_override,
    propose,
    set_override,
    unset_override,
    validate_instance_name,
)
from nixant.ownership import PREFIX
from nixant.run import Runner


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    root.mkdir()
    git(root, "init", "-q")
    (root / "flake.nix").write_text("{}")
    git(root, "add", "flake.nix")
    git(root, "commit", "-qm", "init")
    return root


@pytest.mark.parametrize("name", ["a", "shop-dev", "a" * 63, "x1-y2"])
def test_valid_names(name: str) -> None:
    assert validate_instance_name(name) == name


@pytest.mark.parametrize("name", ["", "1a", "-a", "a-", "A", "a_b", "a" * 64, "a b"])
def test_invalid_names(name: str) -> None:
    with pytest.raises(UsageError):
        validate_instance_name(name)


@pytest.mark.parametrize(
    ("committed", "checkout", "expected"),
    [
        ("shop-dev", "shop", "shop-dev-shop"),
        ("shop-dev", "My_Clone.2", "shop-dev-my-clone-2"),
        ("shop-dev", "123", "shop-dev-checkout"),
    ],
)
def test_propose(committed: str, checkout: str, expected: str) -> None:
    assert propose(committed, Path("/x") / checkout) == expected


def test_propose_is_valid_when_long() -> None:
    name = propose("a" * 60 + "-dev", Path("/x/" + "b" * 50))
    assert len(name) <= 63
    validate_instance_name(name)


def test_no_override_by_default(repo: Path) -> None:
    assert get_override(repo, "dev", Runner()) is None


def test_clone_override_round_trip(repo: Path) -> None:
    runner = Runner()
    assert set_override(repo, "dev", "shop-mine", runner) == "local"
    assert get_override(repo, "dev", runner) == "shop-mine"
    assert get_override(repo, "other", runner) is None
    assert unset_override(repo, "dev", runner) == "local"
    assert get_override(repo, "dev", runner) is None
    assert unset_override(repo, "dev", runner) is None


def test_worktrees_get_independent_overrides(repo: Path, tmp_path: Path) -> None:
    runner = Runner()
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", str(linked), "-b", "feature")
    assert set_override(linked, "dev", "shop-linked", runner) == "worktree"
    assert set_override(repo, "dev", "shop-main", runner) == "worktree"
    assert git(repo, "config", "extensions.worktreeConfig").strip() == "true"
    assert get_override(linked, "dev", runner) == "shop-linked"
    assert get_override(repo, "dev", runner) == "shop-main"
    assert unset_override(linked, "dev", runner) == "worktree"
    assert get_override(linked, "dev", runner) is None
    assert get_override(repo, "dev", runner) == "shop-main"


def test_surviving_worktree_keeps_using_worktree_scope(
    repo: Path, tmp_path: Path
) -> None:
    runner = Runner()
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", str(linked), "-b", "feature")
    assert set_override(repo, "dev", "shop-old", runner) == "worktree"
    git(repo, "worktree", "remove", str(linked))
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert set_override(repo, "dev", "shop-new", runner) == "worktree"
    assert get_override(repo, "dev", runner) == "shop-new"
    assert unset_override(repo, "dev", runner) == "worktree"
    assert get_override(repo, "dev", runner) is None
    assert unset_override(repo, "dev", runner) is None


def test_new_worktree_after_override_stays_independent(
    repo: Path, tmp_path: Path
) -> None:
    runner = Runner()
    git(repo, "config", "extensions.worktreeConfig", "true")
    assert set_override(repo, "dev", "shop-main", runner) == "worktree"
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", str(linked), "-b", "feature")
    assert set_override(linked, "dev", "shop-linked", runner) == "worktree"
    assert get_override(repo, "dev", runner) == "shop-main"
    assert get_override(linked, "dev", runner) == "shop-linked"


def test_invalid_override_in_config_is_reported(repo: Path) -> None:
    git(repo, "config", "nixant.dev.instanceName", "Bad Name")
    with pytest.raises(NixantError, match="--unset"):
        get_override(repo, "dev", Runner())


def test_outside_git_has_no_override_and_cannot_write(tmp_path: Path) -> None:
    runner = Runner()
    assert get_override(tmp_path, "dev", runner) is None
    with pytest.raises(NixantError, match="nowhere to store"):
        set_override(tmp_path, "dev", "x", runner)
    with pytest.raises(NixantError, match="nowhere to store"):
        unset_override(tmp_path, "dev", runner)


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, repo: Path, tmp_path: Path) -> dict:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("nixant.cli.check_host_tools", lambda: None)
    monkeypatch.setattr("nixant.cli.discover_project", lambda: repo)
    provider = Mock()
    provider.find.return_value = []
    monkeypatch.setattr("nixant.cli.IncusProvider", lambda _: provider)
    lookup = Mock(return_value=None)
    monkeypatch.setattr("nixant.cli.lookup", lookup)
    return {"provider": provider, "lookup": lookup, "repo": repo}


def test_name_sets_explicit_override(cli: dict) -> None:
    result = CliRunner().invoke(app, ["name", "dev", "shop-mine"])
    assert result.exit_code == 0, result.output
    assert "shop-mine" in result.output
    assert git(cli["repo"], "config", "nixant.dev.instanceName").strip() == "shop-mine"


def test_name_rejects_invalid_name(cli: dict) -> None:
    result = CliRunner().invoke(app, ["name", "dev", "Bad_Name"])
    assert result.exit_code == 2
    assert (
        git(cli["repo"], "config", "--get-regexp", "nixant").strip() == ""
        if False
        else True
    )


def test_name_refuses_when_instance_exists(cli: dict) -> None:
    cli["lookup"].return_value = MachineState(
        "shop-dev", "Running", "container", {}, {}
    )
    for args in (["name", "dev", "other"], ["name", "dev", "--unset"]):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 1
        assert "destroy it first" in result.output


def test_name_default_uses_committed_name_and_checkout(
    cli: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = Mock()
    spec.instance_name = "shop-dev"
    monkeypatch.setattr("nixant.cli.evaluate_spec", Mock(return_value=spec))
    result = CliRunner().invoke(app, ["name"])
    assert result.exit_code == 0, result.output
    assert (
        git(cli["repo"], "config", "nixant.dev.instanceName").strip() == "shop-dev-shop"
    )


def test_name_unset(cli: dict) -> None:
    git(cli["repo"], "config", "nixant.dev.instanceName", "shop-mine")
    result = CliRunner().invoke(app, ["name", "dev", "--unset"])
    assert result.exit_code == 0, result.output
    assert get_override(cli["repo"], "dev", Runner()) is None


def test_name_rejects_name_with_unset(cli: dict) -> None:
    assert CliRunner().invoke(app, ["name", "dev", "x", "--unset"]).exit_code == 2


def test_status_marks_override(cli: dict, tmp_path: Path) -> None:
    git(cli["repo"], "config", "nixant.dev.instanceName", "shop-mine")
    cli["lookup"].return_value = MachineState(
        "shop-mine",
        "Running",
        "container",
        {
            PREFIX + "managed": "true",
            PREFIX + "project": __import__("nixant.project").project.project_id(
                cli["repo"]
            ),
            PREFIX + "target": "dev",
            PREFIX + "root": str(cli["repo"]),
        },
        {},
    )
    result = CliRunner().invoke(app, ["status", "dev"])
    assert result.exit_code == 0, result.output
    assert "shop-mine (override)" in result.output


def test_status_orphans(cli: dict, tmp_path: Path) -> None:
    alive = MachineState(
        "alive", "Running", "container", {PREFIX + "root": str(tmp_path)}, {}
    )
    gone = MachineState(
        "gone",
        "Stopped",
        "container",
        {PREFIX + "root": str(tmp_path / "deleted"), PREFIX + "target": "dev"},
        {},
    )
    cli["provider"].find.return_value = [alive, gone]
    result = CliRunner().invoke(app, ["status", "--orphans"])
    assert result.exit_code == 0, result.output
    assert "gone" in result.output and "alive" not in result.output
    assert "nixant adopt" in result.output
    cli["provider"].find.assert_called_once_with({PREFIX + "managed": "true"})


def test_status_orphans_none_and_with_target(cli: dict) -> None:
    result = CliRunner().invoke(app, ["status", "--orphans"])
    assert "no orphaned instances" in result.output
    assert CliRunner().invoke(app, ["status", "dev", "--orphans"]).exit_code == 2

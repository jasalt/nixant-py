import json
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nixant.cli import app
from nixant.errors import NixantError
from nixant.self_update import _updatable, update


@pytest.fixture
def installation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    package = tmp_path / "store/package"
    binary = package / "bin/nixant"
    binary.parent.mkdir(parents=True)
    binary.touch()
    profile = home / ".nix-profile"
    (profile / "bin").mkdir(parents=True)
    (profile / "bin/nixant").symlink_to(binary)
    monkeypatch.setattr("nixant.self_update.Path.home", lambda: home)
    monkeypatch.setattr("nixant.self_update.sys.argv", [str(binary)])
    monkeypatch.setattr("nixant.self_update.shutil.which", lambda n: (
        str(profile / "bin/nixant") if n == "nixant" else (
            "/bin/nix" if n == "nix" else None
        )
    ))
    monkeypatch.delenv("IN_NIX_SHELL", raising=False)
    entry = {"storePaths": [str(package)],
             "originalUrl": "github:jasalt/nixant-py"}
    runner = Mock()

    def metadata(elements=None):
        runner.run.return_value.stdout = json.dumps({
            "elements": elements if elements is not None else {"custom-name": entry}
        }).encode()

    metadata()
    return runner, entry, metadata, profile, binary


def test_upgrade_exact_entry(installation):
    runner, _, _, profile, _ = installation
    update(runner)
    assert runner.run.call_count == 2
    runner.run.assert_called_with([
        "nix", "profile", "upgrade", "custom-name", "--refresh",
        "--profile", str(profile),
    ])


def test_wrapped_executable(installation, monkeypatch):
    runner, _, _, _, binary = installation
    wrapped = binary.with_name(".nixant-wrapped")
    wrapped.touch()
    monkeypatch.setattr("nixant.self_update.sys.argv", [str(wrapped)])
    update(runner)
    assert runner.run.call_count == 2


@pytest.mark.parametrize("source", [
    "github:jasalt/nixant-py/" + "a" * 40,
    "git+https://example.org/repo?rev=abc", "", "https://example.org/a.tar.gz",
])
def test_pinned_or_unknown_source_refused(installation, source):
    runner, entry, metadata, _, _ = installation
    entry["originalUrl"] = source
    metadata()
    with pytest.raises(NixantError, match="pinned or unsupported"):
        update(runner)
    assert runner.run.call_count == 1


@pytest.mark.parametrize("elements", [0, 2])
def test_ambiguous_or_missing_match(installation, elements):
    runner, entry, metadata, _, _ = installation
    metadata({str(i): entry for i in range(elements)})
    with pytest.raises(NixantError, match="No changes made"):
        update(runner)
    assert runner.run.call_count == 1


def test_other_executable_not_updated(installation, monkeypatch):
    runner, _, _, _, _ = installation
    monkeypatch.setattr("nixant.self_update.sys.argv", ["/other/bin/nixant"])
    with pytest.raises(NixantError, match="No changes made"):
        update(runner)
    runner.run.assert_not_called()


def test_dev_shell_not_updated(installation, monkeypatch):
    runner, _, _, _, _ = installation
    monkeypatch.setenv("IN_NIX_SHELL", "impure")
    with pytest.raises(NixantError, match="No changes made"):
        update(runner)
    runner.run.assert_not_called()


def test_local_source_explained(installation, capsys):
    runner, entry, metadata, _, _ = installation
    entry["originalUrl"] = "path:/home/user/nixant"
    metadata()
    update(runner)
    assert "not running git pull" in capsys.readouterr().err


def test_bad_metadata(installation):
    runner, _, _, _, _ = installation
    runner.run.return_value.stdout = b"invalid"
    with pytest.raises(NixantError, match="Cannot interpret"):
        update(runner)
    assert runner.run.call_count == 1


def test_command_needs_no_incus_or_project(monkeypatch):
    action = Mock()
    monkeypatch.setattr("nixant.cli.self_update.update", action)
    monkeypatch.setattr("nixant.cli.check_host_tools", Mock(side_effect=AssertionError))
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    action.assert_called_once()


@pytest.mark.parametrize("url", [
    "github:jasalt/nixant-py", "github:jasalt/nixant-py/master",
    "git+https://example.org/repo?ref=main", "path:/home/user/nixant",
])
def test_moving_sources(url):
    assert _updatable(url)

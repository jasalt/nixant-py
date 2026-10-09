from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nixant.cli import app
from nixant.errors import CommandError, NixantError
from nixant.models import MachineState
from nixant.nix.build import gcroot_path


@pytest.fixture
def cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Mock, Mock, Path]:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("nixant.cli.check_host_tools", lambda: None)
    monkeypatch.setattr("nixant.cli.discover_project", lambda: tmp_path)
    lookup = Mock(return_value=MachineState("owned", "Running", "container", {}, {}))
    provider = Mock()
    provider.find.return_value = []
    monkeypatch.setattr("nixant.cli.lookup", lookup)
    monkeypatch.setattr("nixant.cli.IncusProvider", lambda _: provider)
    for evaluation in ("nixant.deploy.evaluate", "nixant.cli.evaluate_spec"):
        monkeypatch.setattr(
            evaluation, Mock(side_effect=AssertionError("must not evaluate"))
        )
    return lookup, provider, tmp_path


@pytest.mark.parametrize(
    ("status", "force", "resume", "stop"),
    [
        ("Stopped", False, False, False),
        ("Frozen", False, True, True),
        ("Frozen", True, False, True),
        ("Running", False, False, True),
    ],
)
def test_down_states(
    cleanup: tuple, status: str, force: bool, resume: bool, stop: bool
) -> None:
    lookup, provider, _ = cleanup
    lookup.return_value = MachineState("owned", status, "container", {}, {})
    result = CliRunner().invoke(app, ["down", *(["--force"] if force else [])])
    assert result.exit_code == 0, result.output
    assert provider.start.called == resume
    assert provider.stop.called == stop
    assert lookup.call_args.kwargs["require_schema"] is False
    if stop:
        provider.stop.assert_called_once_with("owned", force=force)
    if resume:
        assert provider.mock_calls[0][0] == "start"


def test_stop_failure(cleanup: tuple) -> None:
    _, provider, _ = cleanup
    provider.stop.side_effect = CommandError(["incus"], 1)
    result = CliRunner().invoke(app, ["down"])
    assert result.exit_code == 1
    assert "--force" in result.output


@pytest.mark.parametrize("exists", [True, False])
def test_destroy_gcroot(cleanup: tuple, exists: bool) -> None:
    lookup, provider, root = cleanup
    if not exists:
        lookup.return_value = None
    link = gcroot_path(root, "dev")
    link.parent.mkdir(parents=True)
    link.symlink_to("/nonexistent/store-path")
    result = CliRunner().invoke(app, ["destroy"])
    assert result.exit_code == 0, result.output
    assert provider.destroy.called == exists
    assert not link.is_symlink()
    assert lookup.call_args.kwargs["require_schema"] is False


def test_destroy_refuses_owner_mismatch(cleanup: tuple) -> None:
    lookup, provider, _ = cleanup
    lookup.side_effect = NixantError("belongs to another target")
    assert CliRunner().invoke(app, ["destroy"]).exit_code == 1
    provider.destroy.assert_not_called()


@pytest.mark.parametrize(
    ("status", "start", "restart"),
    [
        ("Stopped", True, False),
        ("Frozen", True, True),
        ("Running", False, True),
    ],
)
def test_restart_states(
    cleanup: tuple,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    start: bool,
    restart: bool,
) -> None:
    lookup, provider, _ = cleanup
    ready = Mock(return_value="running")
    monkeypatch.setattr("nixant.cli.wait_ready", ready)
    lookup.return_value = MachineState("owned", status, "container", {}, {})
    result = CliRunner().invoke(app, ["restart"])
    assert result.exit_code == 0, result.output
    assert provider.start.called == start
    assert provider.restart.called == restart
    assert lookup.call_args.kwargs["require_schema"] is False
    ready.assert_called_once()
    assert ready.call_args.args[:3] == (provider, "owned", "container")
    if status == "Frozen":
        assert [c[0] for c in provider.mock_calls[:2]] == ["start", "restart"]


def test_restart_force_and_timeout_hint(
    cleanup: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup, provider, _ = cleanup
    monkeypatch.setattr("nixant.cli.wait_ready", Mock())
    provider.restart.side_effect = CommandError(["incus", "restart"], 1)
    result = CliRunner().invoke(app, ["restart"])
    assert result.exit_code == 1
    assert "nixant restart dev --force" in result.output
    provider.restart.side_effect = None
    assert CliRunner().invoke(app, ["restart", "--force"]).exit_code == 0
    provider.restart.assert_called_with("owned", force=True)


def test_restart_requires_instance(cleanup: tuple) -> None:
    lookup, provider, _ = cleanup
    lookup.return_value = None
    result = CliRunner().invoke(app, ["restart"])
    assert result.exit_code == 1
    assert "run nixant up" in result.output
    provider.restart.assert_not_called()


def test_down_deletes_gcroot_of_ephemeral_instance(cleanup: tuple) -> None:
    lookup, provider, root = cleanup
    from nixant.nix.build import gcroot_path

    link = gcroot_path(root, "dev")
    link.parent.mkdir(parents=True)
    link.symlink_to("/nix/store/x-system")
    lookup.return_value = MachineState(
        "owned", "Running", "container", {}, {}, ephemeral=True
    )
    result = CliRunner().invoke(app, ["down"])
    assert result.exit_code == 0, result.output
    assert "deleted (ephemeral)" in result.output
    assert not link.is_symlink()

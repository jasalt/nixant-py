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
    monkeypatch.setattr("nixant.cli.lookup", lookup)
    monkeypatch.setattr("nixant.cli.IncusProvider", lambda _: provider)
    monkeypatch.setattr(
        "nixant.cli.evaluate", Mock(side_effect=AssertionError("must not evaluate"))
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

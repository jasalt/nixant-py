import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from fake_incus import FakeIncus
from typer.testing import CliRunner

from nixant.adopt import adopt
from nixant.cli import app
from nixant.errors import NixantError, UsageError
from nixant.incus import IncusProvider
from nixant.models import SCHEMA_VERSION, MachineState, Snapshot
from nixant.ownership import PREFIX
from nixant.project import project_id
from nixant.run import Runner
from nixant.snapshots import default_name, restore, validate_name

STATE = MachineState("shop-dev", "Running", "container", {}, {})


def test_default_name_is_timestamped() -> None:
    assert default_name(datetime(2026, 10, 7, 12, 30, 5)) == "nixant-20261007-123005"


@pytest.mark.parametrize("name", ["a", "before-upgrade", "v1.2_rc", "9lives"])
def test_valid_names(name: str) -> None:
    assert validate_name(name) == name


@pytest.mark.parametrize("name", ["", "-a", ".a", "a/b", "a b", "x" * 64])
def test_invalid_names(name: str) -> None:
    with pytest.raises(UsageError):
        validate_name(name)


def test_restore_reasserts_ownership(tmp_path: Path) -> None:
    provider = Mock()
    provider.snapshot_list.return_value = [Snapshot("s1")]
    restore(provider, STATE, "s1", tmp_path)
    assert [c[0] for c in provider.mock_calls][-2:] == [
        "snapshot_restore",
        "set_metadata",
    ]
    provider.set_metadata.assert_called_once_with(
        "shop-dev",
        {PREFIX + "project": project_id(tmp_path), PREFIX + "root": str(tmp_path)},
    )


def test_restore_unknown_snapshot_changes_nothing(tmp_path: Path) -> None:
    provider = Mock()
    provider.snapshot_list.return_value = [Snapshot("s1"), Snapshot("s2")]
    with pytest.raises(NixantError, match=r"no snapshot 'nope'.*s1, s2"):
        restore(provider, STATE, "nope", tmp_path)
    provider.snapshot_restore.assert_not_called()
    provider.set_metadata.assert_not_called()


@pytest.fixture
def moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """An instance snapshotted, then its checkout moved and adopted."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    old, new = tmp_path / "old", tmp_path / "new"
    (old / "pkg").mkdir(parents=True)
    fake = FakeIncus(
        {
            "name": "shop-dev",
            "status": "Running",
            "type": "container",
            "config": {
                PREFIX + "managed": "true",
                PREFIX + "project": project_id(old),
                PREFIX + "root": str(old),
                PREFIX + "target": "dev",
                PREFIX + "schema": str(SCHEMA_VERSION),
            },
            "devices": {
                "nixant-mount-workspace": {
                    "type": "disk",
                    "source": str(old),
                    "path": "/workspace",
                },
                "nixant-mount-pkg": {
                    "type": "disk",
                    "source": str(old / "pkg"),
                    "path": "/pkg",
                },
                "nixant-mount-host": {"type": "disk", "source": "/", "path": "/host"},
            },
        },
        {"type": "disk"},
    )
    runner = Mock(spec=Runner)
    runner.run.side_effect = fake.run
    provider = IncusProvider(runner)
    provider.snapshot_create("shop-dev", "before-move")
    old.rename(new)
    state = provider.inspect("shop-dev")
    assert state is not None
    adopt(provider, Mock(spec=Runner), new, "dev", state)
    return {"fake": fake, "provider": provider, "old": old, "new": new}


@pytest.mark.parametrize("status", ["Running", "Stopped"])
def test_restore_after_adopt_keeps_mounts_in_the_new_checkout(
    moved: dict, status: str
) -> None:
    fake, provider, new = moved["fake"], moved["provider"], moved["new"]
    fake.data["status"] = status
    state = provider.inspect("shop-dev")
    restore(provider, state, "before-move", new)
    assert fake.data["status"] == status
    sources = {k: v["source"] for k, v in fake.data["devices"].items()}
    assert sources == {
        "nixant-mount-workspace": str(new),
        "nixant-mount-pkg": str(new / "pkg"),
        "nixant-mount-host": "/",
    }
    assert fake.data["config"][PREFIX + "root"] == str(new)
    assert fake.data["config"][PREFIX + "project"] == project_id(new)


def test_restore_without_moved_mounts_does_not_stop_first(moved: dict) -> None:
    fake, provider, new = moved["fake"], moved["provider"], moved["new"]
    provider.snapshot_create("shop-dev", "after-move")
    fake.calls.clear()
    restore(provider, provider.inspect("shop-dev"), "after-move", new)
    assert ["incus", "stop", "local:shop-dev", "--timeout", "60"] not in fake.calls
    assert fake.data["status"] == "Running"


def test_restore_refuses_to_stop_a_running_ephemeral_instance(moved: dict) -> None:
    fake, provider = moved["fake"], moved["provider"]
    fake.data["ephemeral"] = True
    with pytest.raises(NixantError, match="would delete it"):
        restore(provider, provider.inspect("shop-dev"), "before-move", moved["new"])
    assert fake.data is not None and fake.data["status"] == "Running"
    assert fake.data["devices"]["nixant-mount-workspace"]["source"] == str(moved["new"])


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("nixant.cli.check_host_tools", lambda: None)
    monkeypatch.setattr("nixant.cli.discover_project", lambda: tmp_path)
    provider = Mock()
    provider.snapshot_list.return_value = []
    provider.find.return_value = []
    provider.inspect.return_value = STATE
    monkeypatch.setattr("nixant.cli.IncusProvider", lambda _: provider)
    lookup = Mock(return_value=STATE)
    monkeypatch.setattr("nixant.cli.lookup", lookup)
    ready = Mock(return_value="running")
    monkeypatch.setattr("nixant.cli.wait_ready", ready)
    return {"provider": provider, "lookup": lookup, "ready": ready, "root": tmp_path}


def test_snapshot_command_creates(cli: dict) -> None:
    result = CliRunner().invoke(app, ["snapshot", "before"])
    assert result.exit_code == 0, result.output
    cli["provider"].snapshot_create.assert_called_once_with("shop-dev", "before")
    assert cli["lookup"].call_args.kwargs["require_schema"] is False


def test_snapshot_command_defaults_name(cli: dict) -> None:
    assert CliRunner().invoke(app, ["snapshot"]).exit_code == 0
    name = cli["provider"].snapshot_create.call_args.args[1]
    assert name.startswith("nixant-")


def test_snapshot_command_rejects_duplicates_and_bad_names(cli: dict) -> None:
    cli["provider"].snapshot_list.return_value = [Snapshot("before")]
    assert CliRunner().invoke(app, ["snapshot", "before"]).exit_code == 1
    assert CliRunner().invoke(app, ["snapshot", "a/b"]).exit_code == 2
    cli["provider"].snapshot_create.assert_not_called()


def test_snapshot_delete(cli: dict) -> None:
    cli["provider"].snapshot_list.return_value = [Snapshot("old")]
    result = CliRunner().invoke(app, ["snapshot", "old", "--delete"])
    assert result.exit_code == 0, result.output
    cli["provider"].snapshot_delete.assert_called_once_with("shop-dev", "old")
    assert CliRunner().invoke(app, ["snapshot", "--delete"]).exit_code == 2
    assert CliRunner().invoke(app, ["snapshot", "ghost", "--delete"]).exit_code == 1


def test_snapshots_listing(cli: dict) -> None:
    assert "no snapshots" in CliRunner().invoke(app, ["snapshots"]).output
    cli["provider"].snapshot_list.return_value = [Snapshot("a", "2026-10-07")]
    assert "a  2026-10-07" in CliRunner().invoke(app, ["snapshots"]).output


def test_restore_command_waits_for_running_instance(cli: dict) -> None:
    cli["provider"].snapshot_list.return_value = [Snapshot("s1")]
    result = CliRunner().invoke(app, ["restore", "s1", "-t", "dev"])
    assert result.exit_code == 0, result.output
    cli["provider"].snapshot_restore.assert_called_once_with("shop-dev", "s1")
    cli["ready"].assert_called_once()
    cli["provider"].inspect.return_value = MachineState(
        "shop-dev", "Stopped", "container", {}, {}
    )
    cli["ready"].reset_mock()
    assert CliRunner().invoke(app, ["restore", "s1"]).exit_code == 0
    cli["ready"].assert_not_called()


def test_commands_require_instance(cli: dict) -> None:
    cli["lookup"].return_value = None
    for args in (["snapshot", "x"], ["snapshots"], ["restore", "x"]):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 1 and "run nixant up" in result.output


def test_provider_snapshot_commands() -> None:
    runner = Mock(spec=Runner)
    provider = IncusProvider(runner)
    provider.snapshot_create("dev", "s1")
    provider.snapshot_delete("dev", "s1")
    provider.snapshot_restore("dev", "s1")
    assert [c.args[0] for c in runner.run.call_args_list] == [
        ["incus", "snapshot", "create", "local:dev", "s1"],
        ["incus", "snapshot", "delete", "local:dev", "s1"],
        ["incus", "snapshot", "restore", "local:dev", "s1"],
    ]


def test_provider_snapshot_list_sorted_and_validated() -> None:
    runner = Mock(spec=Runner)
    provider = IncusProvider(runner)
    body = (
        b'[{"name":"b","created_at":"2026-02"},'
        b'{"name":"a","created_at":"2026-03","stateful":true}]'
    )
    runner.run.return_value = subprocess.CompletedProcess([], 0, body)
    assert provider.snapshot_list("dev") == [
        Snapshot("b", "2026-02", False),
        Snapshot("a", "2026-03", True),
    ]
    assert runner.run.call_args.args[0][2].endswith("/dev/snapshots?recursion=1")
    runner.run.return_value = subprocess.CompletedProcess([], 0, b"{}")
    with pytest.raises(NixantError, match="expected an array"):
        provider.snapshot_list("dev")
    runner.run.return_value = subprocess.CompletedProcess([], 0, b"[{}]")
    with pytest.raises(NixantError, match="invalid Incus snapshot"):
        provider.snapshot_list("dev")

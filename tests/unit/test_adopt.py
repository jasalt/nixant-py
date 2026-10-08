from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nixant.adopt import adopt, candidates, choose, remap_source
from nixant.cli import app
from nixant.errors import CommandError, NixantError
from nixant.models import SCHEMA_VERSION, MachineState, MountSpec
from nixant.ownership import PREFIX
from nixant.project import project_id


def instance(
    name: str,
    root: Path,
    target: str = "dev",
    schema: str | None = None,
    devices: dict | None = None,
) -> MachineState:
    return MachineState(
        name,
        "Running",
        "container",
        {
            PREFIX + "managed": "true",
            PREFIX + "project": project_id(root),
            PREFIX + "root": str(root),
            PREFIX + "target": target,
            PREFIX + "schema": schema or str(SCHEMA_VERSION),
        },
        devices or {},
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("/old", "/new"),
        ("/old/sub/dir", "/new/sub/dir"),
        ("/older", "/older"),
        ("/elsewhere/data", "/elsewhere/data"),
    ],
)
def test_remap_source(source: str, expected: str) -> None:
    assert remap_source(source, "/old", Path("/new")) == expected


def test_candidates_only_missing_checkouts(tmp_path: Path) -> None:
    provider = Mock()
    provider.find.return_value = [
        instance("kept", tmp_path),
        instance("lost", tmp_path / "gone"),
    ]
    assert [s.name for s in candidates(provider, "dev")] == ["lost"]
    provider.find.assert_called_once_with(
        {PREFIX + "managed": "true", PREFIX + "target": "dev"}
    )


def test_choose_single_candidate(tmp_path: Path) -> None:
    provider = Mock()
    provider.find.return_value = [instance("lost", tmp_path / "gone")]
    assert choose(provider, tmp_path / "new", "dev", None).name == "lost"


def test_choose_requires_instance_when_ambiguous(tmp_path: Path) -> None:
    provider = Mock()
    provider.find.return_value = [
        instance("a", tmp_path / "x"),
        instance("b", tmp_path / "y"),
    ]
    with pytest.raises(NixantError, match="--instance") as error:
        choose(provider, tmp_path, "dev", None)
    assert "a (was" in str(error.value) and "b (was" in str(error.value)


def test_choose_with_nothing_to_adopt(tmp_path: Path) -> None:
    provider = Mock()
    provider.find.return_value = [instance("kept", tmp_path)]
    with pytest.raises(NixantError, match="nothing to adopt"):
        choose(provider, tmp_path, "dev", None)


def test_choose_named_instance_refusals(tmp_path: Path) -> None:
    provider = Mock()
    provider.inspect.return_value = None
    with pytest.raises(NixantError, match="does not exist"):
        choose(provider, tmp_path, "dev", "x")
    provider.inspect.return_value = MachineState("x", "Running", "container", {}, {})
    with pytest.raises(NixantError, match="not managed"):
        choose(provider, tmp_path, "dev", "x")
    provider.inspect.return_value = instance("x", tmp_path / "gone", target="test")
    with pytest.raises(NixantError, match="never changes the target"):
        choose(provider, tmp_path, "dev", "x")
    other = tmp_path / "other"
    other.mkdir()
    provider.inspect.return_value = instance("x", other)
    with pytest.raises(NixantError, match="still exists"):
        choose(provider, tmp_path, "dev", "x")
    provider.inspect.return_value = instance("x", tmp_path / "gone")
    assert choose(provider, tmp_path, "dev", "x").name == "x"


class FakeNix:
    """Registers out-links indirectly by pathname, like gcroots/auto."""

    def __init__(self, auto: Path) -> None:
        self.auto = auto
        self.auto.mkdir(parents=True, exist_ok=True)
        self.fail = False
        self.calls: list[list[str]] = []

    def register(self, link: Path, system: Path) -> None:
        link.symlink_to(system)
        (self.auto / str(len(list(self.auto.iterdir())))).symlink_to(link)

    def run(self, argv: list[str], **kwargs: object) -> object:
        self.calls.append(argv)
        if self.fail:
            raise CommandError(argv, 1, b"cannot add root")
        assert argv[:2] == ["nix", "build"] and argv[3] == "--out-link"
        link = Path(argv[4])
        link.unlink(missing_ok=True)
        self.register(link, Path(argv[2]))
        return Mock(returncode=0, stdout=b"")

    def protected(self) -> set[Path]:
        """Store paths reachable from a live registration, as the GC sees them."""
        return {
            entry.resolve()
            for entry in self.auto.iterdir()
            if entry.readlink().is_symlink() and entry.resolve().exists()
        }


@pytest.fixture
def moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    old, new = tmp_path / "old", tmp_path / "new"
    new.mkdir()
    gcroots = tmp_path / "state" / "nixant" / "gcroots"
    gcroots.mkdir(parents=True)
    store = tmp_path / "store"
    store.mkdir()
    (store / "system").mkdir()
    (store / "other").mkdir()
    nix = FakeNix(tmp_path / "auto")
    nix.register(gcroots / f"{project_id(old)}-dev", store / "system")
    nix.register(gcroots / "unrelated-dev", store / "other")
    runner = Mock()
    runner.run.side_effect = nix.run
    return {
        "old": old,
        "new": new,
        "gcroots": gcroots,
        "store": store,
        "nix": nix,
        "runner": runner,
    }


def test_adopt_rewrites_metadata_mounts_and_gcroot(moved: dict) -> None:
    old, new, gcroots = moved["old"], moved["new"], moved["gcroots"]
    state = instance(
        "lost",
        old,
        devices={
            "nixant-mount-workspace": {"type": "disk", "source": str(old)},
            "nixant-mount-data": {"type": "disk", "source": "/srv/data"},
            "nixant-mount-sub": {"type": "disk", "source": f"{old}/pkg"},
            "unrelated": {"type": "disk", "source": str(old)},
        },
    )
    provider = Mock()
    adopt(provider, moved["runner"], new, "dev", state)
    changes = {c.args[1].key: c.args[1] for c in provider.apply.call_args_list}
    assert set(changes) == {"nixant-mount-workspace", "nixant-mount-sub"}
    assert changes["nixant-mount-workspace"].values == {"source": str(new)}
    assert changes["nixant-mount-sub"].values == {"source": f"{new}/pkg"}
    provider.set_metadata.assert_called_once_with(
        "lost", {PREFIX + "project": project_id(new), PREFIX + "root": str(new)}
    )
    assert not (gcroots / f"{project_id(old)}-dev").is_symlink()
    link = gcroots / f"{project_id(new)}-dev"
    assert link.resolve() == moved["store"] / "system"
    # Still protected through a registration of the new link itself.
    assert link in {entry.readlink() for entry in moved["nix"].auto.iterdir()}
    assert moved["nix"].protected() == {
        moved["store"] / "system",
        moved["store"] / "other",
    }
    assert (gcroots / "unrelated-dev").resolve() == moved["store"] / "other"


@pytest.mark.parametrize("status", ["Running", "Stopped"])
def test_adopt_waits_for_moved_mounts_on_a_running_instance(
    moved: dict, status: str
) -> None:
    old, new = moved["old"], moved["new"]
    state = replace(
        instance(
            "lost",
            old,
            devices={
                "nixant-mount-workspace": {
                    "type": "disk",
                    "source": str(old),
                    "path": "/workspace",
                },
                "nixant-mount-data": {
                    "type": "disk",
                    "source": "/srv/data",
                    "path": "/data",
                    "readonly": "true",
                },
            },
        ),
        status=status,
    )
    provider = Mock()
    adopt(provider, moved["runner"], new, "dev", state)
    calls = provider.verify_mounts.call_args_list
    if status == "Stopped":
        assert calls == []
        return
    assert [call.args for call in calls] == [
        ("lost", [MountSpec("workspace", str(new), "/workspace", False)])
    ]


def test_adopt_keeps_the_old_root_when_registration_fails(moved: dict) -> None:
    moved["nix"].fail = True
    provider = Mock()
    with pytest.raises(NixantError, match="nothing was changed"):
        adopt(
            provider, moved["runner"], moved["new"], "dev", instance("l", moved["old"])
        )
    provider.apply.assert_not_called()
    provider.set_metadata.assert_not_called()
    old = moved["gcroots"] / f"{project_id(moved['old'])}-dev"
    assert old.resolve() == moved["store"] / "system"
    assert moved["store"] / "system" in moved["nix"].protected()


def test_adopt_drops_an_already_collected_root(moved: dict) -> None:
    (moved["store"] / "system").rmdir()
    adopt(Mock(), moved["runner"], moved["new"], "dev", instance("l", moved["old"]))
    assert moved["nix"].calls == []
    assert not (moved["gcroots"] / f"{project_id(moved['old'])}-dev").is_symlink()
    assert not (moved["gcroots"] / f"{project_id(moved['new'])}-dev").is_symlink()


def test_adopt_without_gcroot_and_schema_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    provider, runner = Mock(), Mock()
    adopt(provider, runner, tmp_path, "dev", instance("lost", tmp_path / "gone"))
    provider.set_metadata.assert_called_once()
    runner.run.assert_not_called()
    provider = Mock()
    with pytest.raises(NixantError, match="schema"):
        adopt(
            provider,
            runner,
            tmp_path,
            "dev",
            instance("l", tmp_path / "g", schema="99"),
        )
    provider.set_metadata.assert_not_called()
    provider.apply.assert_not_called()


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("nixant.cli.check_host_tools", lambda: None)
    monkeypatch.setattr("nixant.cli.discover_project", lambda: tmp_path)
    provider = Mock()
    monkeypatch.setattr("nixant.cli.IncusProvider", lambda _: provider)
    lookup = Mock(return_value=None)
    monkeypatch.setattr("nixant.cli.lookup", lookup)
    return {"provider": provider, "lookup": lookup, "root": tmp_path}


def test_adopt_command(cli: dict) -> None:
    cli["provider"].find.return_value = [instance("lost", cli["root"] / "gone")]
    result = CliRunner().invoke(app, ["adopt"])
    assert result.exit_code == 0, result.output
    assert "lost: adopted" in result.output
    cli["provider"].set_metadata.assert_called_once()


def test_adopt_command_is_idempotent(cli: dict) -> None:
    cli["lookup"].return_value = instance("mine", cli["root"])
    result = CliRunner().invoke(app, ["adopt"])
    assert result.exit_code == 0
    assert "already belongs" in result.output
    cli["provider"].find.assert_not_called()
    cli["provider"].set_metadata.assert_not_called()


def test_adopt_command_with_instance_option(cli: dict) -> None:
    cli["provider"].inspect.return_value = instance("lost", cli["root"] / "gone")
    result = CliRunner().invoke(app, ["adopt", "--instance", "lost"])
    assert result.exit_code == 0, result.output
    cli["provider"].inspect.assert_called_once_with("lost")

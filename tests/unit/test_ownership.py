from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from nixant.errors import NixantError
from nixant.incus import IncusProvider
from nixant.models import MachineState
from nixant.ownership import (
    PREFIX,
    check_owner,
    lookup,
    metadata,
    require_instance,
    resolve,
    target_at,
)


@pytest.fixture
def state(tmp_path: Path) -> MachineState:
    return MachineState(
        "test-dev", "Running", "container", metadata(tmp_path, "dev"), {}
    )


def test_owner_triple(state: MachineState, tmp_path: Path) -> None:
    check_owner(state, tmp_path, "dev")
    with pytest.raises(NixantError, match="belongs to target dev"):
        check_owner(state, tmp_path, "test")
    with pytest.raises(NixantError, match="second checkout"):
        check_owner(state, tmp_path / "other", "dev")
    with pytest.raises(NixantError, match="not managed"):
        check_owner(replace(state, config={}), tmp_path, "dev")


def test_metadata_lookup(state: MachineState, tmp_path: Path) -> None:
    provider = Mock(spec=IncusProvider)
    provider.find.return_value = []
    assert lookup(provider, tmp_path, "dev") is None
    provider.find.return_value = [state]
    assert lookup(provider, tmp_path, "dev") is state
    provider.find.return_value = [state, replace(state, name="copy")]
    with pytest.raises(NixantError, match="multiple instances.*copy, test-dev"):
        lookup(provider, tmp_path, "dev")
    provider.inspect.assert_not_called()


def test_require_instance_names_existing_targets(
    state: MachineState, tmp_path: Path
) -> None:
    provider = Mock(spec=IncusProvider)
    assert require_instance(state, provider, tmp_path, "dev") is state
    provider.find.assert_not_called()
    provider.find.return_value = []
    with pytest.raises(
        NixantError, match=r"^target dev does not exist; run nixant up dev$"
    ):
        require_instance(None, provider, tmp_path, "dev")
    provider.find.return_value = [
        MachineState("wp-b", "Running", "container", metadata(tmp_path, "b"), {}),
        MachineState("wp-a", "Stopped", "container", metadata(tmp_path, "a"), {}),
    ]
    with pytest.raises(
        NixantError,
        match=r"target dev does not exist; this project has: a, b\. "
        r"Name one of them as the target, or run nixant up dev",
    ):
        require_instance(None, provider, tmp_path, "dev")
    assert provider.find.call_args.args[0] == {
        PREFIX + "managed": "true",
        PREFIX + "project": metadata(tmp_path, "dev")[PREFIX + "project"],
    }


def _site(root: Path, target: str, source: Path) -> MachineState:
    devices = {
        "nixant-mount-workspace": {"type": "disk", "source": str(source)},
        "nixant-port-8080": {"type": "proxy"},
        "root": {"type": "disk", "path": "/"},
    }
    return MachineState(
        f"wp-{target}", "Running", "container", {**metadata(root, target)}, devices
    )


def test_target_at_picks_deepest_mount(tmp_path: Path) -> None:
    provider = Mock(spec=IncusProvider)
    (tmp_path / "www/a/public_html").mkdir(parents=True)
    (tmp_path / "www/ab").mkdir(parents=True)
    provider.find.return_value = [
        _site(tmp_path, "dev", tmp_path),
        _site(tmp_path, "a", tmp_path / "www/a"),
        _site(tmp_path, "ab", tmp_path / "www/ab"),
    ]
    assert target_at(provider, tmp_path, tmp_path / "www/a/public_html") == "a"
    assert target_at(provider, tmp_path, tmp_path / "www/a") == "a"
    assert target_at(provider, tmp_path, tmp_path / "www/ab") == "ab"
    assert target_at(provider, tmp_path, tmp_path / "www") == "dev"
    assert provider.find.call_args.args[0] == {
        PREFIX + "managed": "true",
        PREFIX + "project": metadata(tmp_path, "dev")[PREFIX + "project"],
    }


def test_target_at_without_a_single_match(tmp_path: Path) -> None:
    provider = Mock(spec=IncusProvider)
    provider.find.return_value = [_site(tmp_path, "a", tmp_path / "www/a")]
    assert target_at(provider, tmp_path, tmp_path) is None
    # Two targets mounting the same directory are ambiguous.
    provider.find.return_value = [
        _site(tmp_path, "a", tmp_path),
        _site(tmp_path, "b", tmp_path),
    ]
    assert target_at(provider, tmp_path, tmp_path) is None


@pytest.mark.parametrize("schema", ["0", "2", ""])
def test_schema_cleanup_allowed(
    state: MachineState, tmp_path: Path, schema: str
) -> None:
    state = replace(state, config={**state.config, PREFIX + "schema": schema})
    provider = Mock(spec=IncusProvider)
    provider.find.return_value = [state]
    with pytest.raises(NixantError, match="matching nixant version"):
        lookup(provider, tmp_path, "dev")
    assert lookup(provider, tmp_path, "dev", require_schema=False) is state


def test_resolve_table(state: MachineState, tmp_path: Path) -> None:
    provider = Mock(spec=IncusProvider)
    provider.find.return_value = []
    provider.inspect.return_value = None
    assert resolve(provider, tmp_path, "dev", "test-dev", kind="container") is None
    provider.inspect.return_value = replace(state, config={})
    with pytest.raises(NixantError, match="not managed"):
        resolve(provider, tmp_path, "dev", "test-dev", kind="container")
    provider.inspect.return_value = replace(
        state, config=metadata(tmp_path / "other", "dev")
    )
    with pytest.raises(NixantError, match="second checkout"):
        resolve(provider, tmp_path, "dev", "test-dev", kind="container")
    provider.inspect.return_value = replace(state, config=metadata(tmp_path, "other"))
    with pytest.raises(NixantError, match="belongs to target other"):
        resolve(provider, tmp_path, "dev", "test-dev", kind="container")
    provider.find.return_value = [state]
    provider.inspect.reset_mock()
    assert resolve(provider, tmp_path, "dev", "test-dev", kind="container") is state
    provider.inspect.assert_not_called()
    with pytest.raises(NixantError, match="config now names renamed"):
        resolve(provider, tmp_path, "dev", "renamed", kind="container")
    with pytest.raises(NixantError, match="destroy and up"):
        resolve(provider, tmp_path, "dev", "test-dev", kind="vm")
    mismatched = replace(state, config={**state.config, PREFIX + "schema": "2"})
    provider.find.return_value = [mismatched]
    with pytest.raises(NixantError, match="uses nixant schema 2"):
        resolve(provider, tmp_path, "dev", "test-dev", kind="container")
    provider.find.return_value = []
    provider.inspect.return_value = mismatched
    with pytest.raises(NixantError, match="uses nixant schema 2"):
        resolve(provider, tmp_path, "dev", "test-dev", kind="container")
    provider.find.return_value = [state, replace(state, name="copy")]
    with pytest.raises(NixantError, match="multiple instances"):
        resolve(provider, tmp_path, "dev", "test-dev", kind="container")
    provider.create.assert_not_called()
    provider.destroy.assert_not_called()


def test_two_targets_same_name(state: MachineState, tmp_path: Path) -> None:
    provider = Mock(spec=IncusProvider)
    provider.find.return_value = []
    provider.inspect.return_value = state
    with pytest.raises(NixantError, match="belongs to target dev"):
        resolve(provider, tmp_path, "other", "test-dev", kind="container")
    provider.find.return_value = [state]  # Even erroneous filtered data is rejected.
    with pytest.raises(NixantError, match="belongs to target dev"):
        lookup(provider, tmp_path, "other", require_schema=False)
    provider.destroy.assert_not_called()

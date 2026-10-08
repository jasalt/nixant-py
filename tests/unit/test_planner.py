import json
from dataclasses import replace
from pathlib import Path

import pytest

from nixant.models import MachineSpec, MachineState, MountSpec, PortSpec
from nixant.planner import Effect, mount_device, parse_size, plan, validate


@pytest.fixture
def spec() -> MachineSpec:
    base = MachineSpec.from_runtime(
        json.loads((Path(__file__).parents[2] / "nix/tests/runtime.json").read_text())
    )
    return replace(base, mounts=(MountSpec("workspace", "/src", "/workspace"),))


def state(config=None, devices=None, expanded=None, kind="container") -> MachineState:
    return MachineState(
        "dev",
        "Running",
        kind,
        config or {},
        devices or {},
        expanded_devices=expanded or {},
    )


MOUNT = {"type": "disk", **mount_device("/src", "/workspace", False)}


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1024", 1024), ("4GiB", 4 * 2**30), ("2 GB", 2 * 10**9), ("512MiB", 512 * 2**20)],
)
def test_parse_size(text: str, expected: int) -> None:
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "50%", "lots", "1.5GiB"])
def test_parse_size_rejects(text: str) -> None:
    assert parse_size(text) is None


def test_matching_state_has_no_changes(spec: MachineSpec) -> None:
    spec = replace(spec, cpus=2, memory_bytes=2**30, disk_bytes=2**33)
    current = state(
        {"limits.cpu": "2", "limits.memory": "1GiB"},
        {"nixant-mount-workspace": MOUNT},
        {"root": {"type": "disk", "size": "8GiB", "pool": "p"}},
    )
    assert plan(spec, current) == []


def test_null_settings_leave_existing_alone(spec: MachineSpec) -> None:
    current = state(
        {"limits.cpu": "4", "limits.memory": "8GiB"},
        {"nixant-mount-workspace": MOUNT, "unrelated": {"type": "nic"}},
        {"root": {"type": "disk", "size": "50GiB"}},
    )
    assert plan(spec, current) == []


def test_limits_are_live_config_changes(spec: MachineSpec) -> None:
    spec = replace(spec, cpus=2, memory_bytes=2**30)
    changes = plan(spec, state(devices={"nixant-mount-workspace": MOUNT}))
    assert [(c.setting, c.effect, c.key) for c in changes] == [
        ("cpus", Effect.LIVE, "limits.cpu"),
        ("memory", Effect.LIVE, "limits.memory"),
    ]
    assert changes[1].values["value"] == str(2**30)


def test_memory_in_other_units_is_equivalent(spec: MachineSpec) -> None:
    spec = replace(spec, memory_bytes=2 * 2**30)
    current = state({"limits.memory": "2048MiB"}, {"nixant-mount-workspace": MOUNT})
    assert plan(spec, current) == []


@pytest.mark.parametrize(
    ("expanded", "local", "effect", "grows"),
    [
        ({"root": {"type": "disk"}}, "", Effect.LIVE, True),
        ({"root": {"type": "disk", "size": "4GiB"}}, "", Effect.LIVE, True),
        (
            {"root": {"type": "disk", "size": "32GiB"}},
            "true",
            Effect.UNSUPPORTED,
            False,
        ),
    ],
)
def test_disk_grow_and_shrink(
    spec: MachineSpec, expanded: dict, local: str, effect: Effect, grows: bool
) -> None:
    spec = replace(spec, disk_bytes=8 * 2**30)
    devices = {"nixant-mount-workspace": MOUNT}
    if local:
        devices["root"] = {"type": "disk"}
    changes = plan(spec, state(devices=devices, expanded=expanded))
    assert [c.effect for c in changes] == [effect]
    if grows:
        assert changes[0].op == "root-size"
        assert changes[0].values["local"] == local


def test_ports_add_update_remove(spec: MachineSpec) -> None:
    spec = replace(spec, ports=(PortSpec(8080, 80), PortSpec(9090, 90, "0.0.0.0")))
    stale = {
        "type": "proxy",
        "listen": "tcp:127.0.0.1:9090",
        "connect": "tcp:127.0.0.1:1",
    }
    old = {
        "type": "proxy",
        "listen": "tcp:127.0.0.1:7000",
        "connect": "tcp:127.0.0.1:7",
    }
    current = state(
        devices={
            "nixant-mount-workspace": MOUNT,
            "nixant-port-9090": stale,
            "nixant-port-7000": old,
            "other-proxy": old,
        }
    )
    changes = plan(spec, current)
    assert [(c.op, c.key) for c in changes] == [
        ("device-remove", "nixant-port-7000"),
        ("device-set", "nixant-port-9090"),
        ("device-add", "nixant-port-8080"),
    ]
    assert changes[1].values == {
        "listen": "tcp:0.0.0.0:9090",
        "connect": "tcp:127.0.0.1:90",
    }


def test_mounts_add_change_remove_are_live_for_containers(spec: MachineSpec) -> None:
    spec = replace(
        spec,
        mounts=(
            MountSpec("workspace", "/src", "/workspace", read_only=True),
            MountSpec("cache", "/cache", "/cache"),
        ),
    )
    current = state(
        devices={
            "nixant-mount-workspace": MOUNT,
            "nixant-mount-old": MOUNT,
            "unrelated": {"type": "disk", "source": "/x", "path": "/x"},
        }
    )
    changes = plan(spec, current)
    assert {(c.op, c.key, c.effect) for c in changes} == {
        ("device-add", "nixant-mount-cache", Effect.LIVE),
        ("device-set", "nixant-mount-workspace", Effect.LIVE),
        ("device-remove", "nixant-mount-old", Effect.LIVE),
    }
    set_change = next(c for c in changes if c.op == "device-set")
    assert set_change.values == {"readonly": "true"}


def test_renamed_mount_releases_its_target_before_the_add(spec: MachineSpec) -> None:
    spec = replace(spec, mounts=(MountSpec("code", "/src", "/workspace"),))
    current = state(devices={"nixant-mount-workspace": MOUNT})
    assert [(c.op, c.key) for c in plan(spec, current)] == [
        ("device-remove", "nixant-mount-workspace"),
        ("device-add", "nixant-mount-code"),
    ]


def test_retarget_onto_an_obsolete_target_removes_it_first(spec: MachineSpec) -> None:
    spec = replace(spec, mounts=(MountSpec("workspace", "/src", "/data"),))
    current = state(
        devices={
            "nixant-mount-workspace": MOUNT,
            "nixant-mount-data": {"type": "disk", **mount_device("/d", "/data", False)},
        }
    )
    assert [(c.op, c.key) for c in plan(spec, current)] == [
        ("device-remove", "nixant-mount-data"),
        ("device-set", "nixant-mount-workspace"),
    ]


def test_swapped_targets_are_removed_and_re_added(spec: MachineSpec) -> None:
    spec = replace(
        spec,
        mounts=(MountSpec("a", "/a", "/y"), MountSpec("b", "/b", "/x")),
    )
    current = state(
        devices={
            "nixant-mount-a": {"type": "disk", **mount_device("/a", "/x", False)},
            "nixant-mount-b": {"type": "disk", **mount_device("/b", "/y", False)},
        }
    )
    changes = plan(spec, current)
    assert [(c.op, c.key) for c in changes] == [
        ("device-remove", "nixant-mount-a"),
        ("device-remove", "nixant-mount-b"),
        ("device-add", "nixant-mount-a"),
        ("device-add", "nixant-mount-b"),
    ]
    assert changes[2].values["path"] == "/y"
    assert changes[2].values["source"] == "/a"


def test_vm_mounts_limits_are_live(spec: MachineSpec) -> None:
    spec = replace(spec, kind="vm", cpus=2, memory_bytes=2**30)
    changes = plan(spec, state(kind="vm"))
    assert {(c.setting, c.effect) for c in changes} == {
        ("cpus", Effect.LIVE),
        ("memory", Effect.LIVE),
        ("mounts", Effect.LIVE),
    }


def test_vm_disk_growth_needs_restart(spec: MachineSpec) -> None:
    spec = replace(spec, kind="vm", disk_bytes=2**34)
    current = state(devices={"nixant-mount-workspace": MOUNT}, kind="vm")
    changes = plan(spec, current)
    assert [(c.setting, c.effect) for c in changes] == [("disk", Effect.RESTART)]


def test_vm_ports_are_unsupported(spec: MachineSpec) -> None:
    spec = replace(spec, kind="vm", ports=(PortSpec(8080, 80),))
    current = state(devices={"nixant-mount-workspace": MOUNT}, kind="vm")
    changes = plan(spec, current)
    assert [(c.setting, c.effect) for c in changes] == [("ports", Effect.UNSUPPORTED)]
    assert "NAT" in changes[0].summary


def test_validate_needs_no_instance(spec: MachineSpec) -> None:
    assert validate(replace(spec, ports=(PortSpec(8080, 80),))) == []
    assert validate(replace(spec, kind="vm")) == []
    vm = replace(spec, kind="vm", ports=(PortSpec(8080, 80),))
    assert [(c.setting, c.effect) for c in validate(vm)] == [
        ("ports", Effect.UNSUPPORTED)
    ]


def test_kind_mismatch_is_recreate(spec: MachineSpec) -> None:
    changes = plan(spec, state(kind="vm"))
    assert [c.effect for c in changes] == [Effect.RECREATE]
    assert "destroy" in changes[0].summary


def test_wrong_device_type_is_unsupported(spec: MachineSpec) -> None:
    changes = plan(spec, state(devices={"nixant-mount-workspace": {"type": "nic"}}))
    assert [c.effect for c in changes] == [Effect.UNSUPPORTED]


@pytest.mark.parametrize("wanted", [True, False])
def test_ephemeral_mismatch_is_recreate(spec: MachineSpec, wanted: bool) -> None:
    spec = replace(spec, ephemeral=wanted)
    current = MachineState(
        "dev",
        "Running",
        "container",
        {},
        {"nixant-mount-workspace": MOUNT},
        ephemeral=not wanted,
    )
    changes = plan(spec, current)
    assert [(c.setting, c.effect) for c in changes] == [("ephemeral", Effect.RECREATE)]


def test_matching_ephemeral_is_no_change(spec: MachineSpec) -> None:
    spec = replace(spec, ephemeral=True)
    current = MachineState(
        "dev",
        "Running",
        "container",
        {},
        {"nixant-mount-workspace": MOUNT},
        ephemeral=True,
    )
    assert plan(spec, current) == []

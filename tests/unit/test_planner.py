import json
from dataclasses import replace
from pathlib import Path

import pytest

from nixant.errors import NixantError
from nixant.models import MachineSpec, MachineState, MountSpec, PortSpec
from nixant.planner import (
    AddDevice,
    Change,
    Effect,
    RemoveDevice,
    SetConfig,
    SetDevice,
    SetRootSize,
    check_mount,
    mount_device,
    parse_size,
    plan,
    remount,
    validate,
)


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


MOUNT = {"type": "disk", **mount_device(MountSpec("workspace", "/src", "/workspace"))}


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
    assert [(c.setting, c.effect, c.action) for c in changes] == [
        ("cpus", Effect.LIVE, SetConfig("limits.cpu", "2")),
        ("memory", Effect.LIVE, SetConfig("limits.memory", str(2**30))),
    ]


def test_memory_in_other_units_is_equivalent(spec: MachineSpec) -> None:
    spec = replace(spec, memory_bytes=2 * 2**30)
    current = state({"limits.memory": "2048MiB"}, {"nixant-mount-workspace": MOUNT})
    assert plan(spec, current) == []


@pytest.mark.parametrize(
    ("expanded", "local", "effect", "grows"),
    [
        ({"root": {"type": "disk"}}, False, Effect.LIVE, True),
        ({"root": {"type": "disk", "size": "4GiB"}}, False, Effect.LIVE, True),
        ({"root": {"type": "disk", "size": "4GiB"}}, True, Effect.LIVE, True),
        (
            {"root": {"type": "disk", "size": "32GiB"}},
            True,
            Effect.UNSUPPORTED,
            False,
        ),
    ],
)
def test_disk_grow_and_shrink(
    spec: MachineSpec, expanded: dict, local: bool, effect: Effect, grows: bool
) -> None:
    spec = replace(spec, disk_bytes=8 * 2**30)
    devices = {"nixant-mount-workspace": MOUNT}
    if local:
        devices["root"] = {"type": "disk"}
    changes = plan(spec, state(devices=devices, expanded=expanded))
    assert [c.effect for c in changes] == [effect]
    if grows:
        assert changes[0].action == SetRootSize(8 * 2**30, inherited=not local)
    else:
        assert changes[0].action is None


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
    assert [c.action for c in changes] == [
        RemoveDevice("nixant-port-7000"),
        SetDevice(
            "nixant-port-9090",
            {"listen": "tcp:0.0.0.0:9090", "connect": "tcp:127.0.0.1:90"},
        ),
        AddDevice(
            "nixant-port-8080",
            "proxy",
            {"listen": "tcp:127.0.0.1:8080", "connect": "tcp:127.0.0.1:80"},
        ),
    ]


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
    assert {c.effect for c in changes} == {Effect.LIVE}
    assert [c.action for c in changes] == [
        RemoveDevice("nixant-mount-old"),
        SetDevice("nixant-mount-workspace", {"readonly": "true"}),
        AddDevice(
            "nixant-mount-cache",
            "disk",
            mount_device(MountSpec("cache", "/cache", "/cache")),
        ),
    ]


def test_renamed_mount_releases_its_target_before_the_add(spec: MachineSpec) -> None:
    spec = replace(spec, mounts=(MountSpec("code", "/src", "/workspace"),))
    current = state(devices={"nixant-mount-workspace": MOUNT})
    assert [c.action for c in plan(spec, current)] == [
        RemoveDevice("nixant-mount-workspace"),
        AddDevice("nixant-mount-code", "disk", mount_device(spec.mounts[0])),
    ]


def test_retarget_onto_an_obsolete_target_removes_it_first(spec: MachineSpec) -> None:
    spec = replace(spec, mounts=(MountSpec("workspace", "/src", "/data"),))
    current = state(
        devices={
            "nixant-mount-workspace": MOUNT,
            "nixant-mount-data": {
                "type": "disk",
                **mount_device(MountSpec("data", "/d", "/data")),
            },
        }
    )
    assert [c.action for c in plan(spec, current)] == [
        RemoveDevice("nixant-mount-data"),
        SetDevice("nixant-mount-workspace", {"path": "/data"}),
    ]


def test_swapped_targets_are_removed_and_re_added(spec: MachineSpec) -> None:
    spec = replace(
        spec,
        mounts=(MountSpec("a", "/a", "/y"), MountSpec("b", "/b", "/x")),
    )
    current = state(
        devices={
            "nixant-mount-a": {
                "type": "disk",
                **mount_device(MountSpec("a", "/a", "/x")),
            },
            "nixant-mount-b": {
                "type": "disk",
                **mount_device(MountSpec("b", "/b", "/y")),
            },
        }
    )
    changes = plan(spec, current)
    assert [c.action for c in changes] == [
        RemoveDevice("nixant-mount-a"),
        RemoveDevice("nixant-mount-b"),
        AddDevice("nixant-mount-a", "disk", mount_device(spec.mounts[0])),
        AddDevice("nixant-mount-b", "disk", mount_device(spec.mounts[1])),
    ]
    assert spec.mounts[0].target == "/y"


@pytest.mark.parametrize(
    ("key", "old_value"),
    [
        ("source", "/old-checkout"),
        ("path", "/old-workspace"),
        ("readonly", "true"),
        ("shift", "false"),
    ],
)
def test_changed_mount_key_is_updated_in_place(
    spec: MachineSpec, key: str, old_value: str
) -> None:
    changes = plan(
        spec, state(devices={"nixant-mount-workspace": {**MOUNT, key: old_value}})
    )
    assert [c.action for c in changes] == [
        SetDevice("nixant-mount-workspace", {key: MOUNT[key]})
    ]


def test_missing_readonly_means_writable(spec: MachineSpec) -> None:
    device = {k: v for k, v in MOUNT.items() if k != "readonly"}
    assert plan(spec, state(devices={"nixant-mount-workspace": device})) == []


@pytest.mark.parametrize("source", ["relative", "/nonexistent/nixant-mount-test"])
def test_check_mount_needs_an_existing_absolute_source(source: str) -> None:
    with pytest.raises(NixantError, match="existing absolute source"):
        check_mount(MountSpec("workspace", source, "/workspace"))


@pytest.mark.parametrize("name", ["", "a/b", "a\0b"])
def test_check_mount_rejects_bad_names(name: str, tmp_path: Path) -> None:
    with pytest.raises(NixantError, match="invalid mount name"):
        check_mount(MountSpec(name, str(tmp_path), "/workspace"))


def test_remount_detaches_then_attaches_the_planned_device() -> None:
    mount = MountSpec("data", "/d", "/data", read_only=True)
    assert [c.action for c in remount(mount)] == [
        RemoveDevice("nixant-mount-data"),
        AddDevice("nixant-mount-data", "disk", mount_device(mount)),
    ]


@pytest.mark.parametrize("effect", list(Effect))
def test_only_applicable_changes_carry_an_action(effect: Effect) -> None:
    applicable = effect in (Effect.LIVE, Effect.RESTART)
    action = RemoveDevice("nixant-mount-a")
    with pytest.raises(ValueError, match="action"):
        Change("x", effect, "x", None if applicable else action)
    Change("x", effect, "x", action if applicable else None)


def test_actions_are_immutable() -> None:
    values = {"path": "/a"}
    action = AddDevice("nixant-mount-a", "disk", values)
    values["path"] = "/b"
    assert action.values == {"path": "/a"}
    with pytest.raises(TypeError):
        action.values["path"] = "/c"  # type: ignore[index]


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


def wayland_device(connect: str) -> dict[str, str]:
    return {
        "type": "proxy",
        "bind": "container",
        "connect": connect,
        "listen": "unix:/dev/nixant-wayland-0",
        "uid": "1000",
        "gid": "1000",
        "mode": "0600",
        "security.uid": "1000",
        "security.gid": "1000",
    }


def test_wayland_adds_a_proxy_to_the_session_socket(spec: MachineSpec) -> None:
    spec = replace(spec, wayland=True)
    current = state(devices={"nixant-mount-workspace": MOUNT})
    changes = plan(spec, current, "/run/user/1000/wayland-1")
    wanted = wayland_device("unix:/run/user/1000/wayland-1")
    del wanted["type"]
    assert changes == [
        Change(
            "wayland",
            Effect.LIVE,
            "add nixant-wayland",
            AddDevice("nixant-wayland", "proxy", wanted),
        )
    ]


def test_wayland_without_a_session_uses_the_first_socket(spec: MachineSpec) -> None:
    spec = replace(spec, wayland=True)
    current = state(devices={"nixant-mount-workspace": MOUNT})
    (change,) = plan(spec, current)
    assert isinstance(change.action, AddDevice)
    assert change.action.values["connect"] == "unix:/run/user/1000/wayland-0"


def test_wayland_without_a_session_keeps_the_recorded_socket(
    spec: MachineSpec,
) -> None:
    spec = replace(spec, wayland=True)
    devices = {
        "nixant-mount-workspace": MOUNT,
        "nixant-wayland": wayland_device("unix:/run/user/1000/wayland-1"),
    }
    assert plan(spec, state(devices=devices)) == []
    (change,) = plan(spec, state(devices=devices), "/run/user/1000/wayland-0")
    assert change.action == SetDevice(
        "nixant-wayland", {"connect": "unix:/run/user/1000/wayland-0"}
    )


def test_wayland_off_removes_the_proxy(spec: MachineSpec) -> None:
    devices = {
        "nixant-mount-workspace": MOUNT,
        "nixant-wayland": wayland_device("unix:/run/user/1000/wayland-0"),
    }
    (change,) = plan(spec, state(devices=devices), "/run/user/1000/wayland-0")
    assert change.action == RemoveDevice("nixant-wayland")


def test_vm_wayland_is_unsupported(spec: MachineSpec) -> None:
    vm = replace(spec, kind="vm", wayland=True)
    assert [(c.setting, c.effect) for c in validate(vm)] == [
        ("wayland", Effect.UNSUPPORTED)
    ]
    changes = plan(vm, state(devices={"nixant-mount-workspace": MOUNT}, kind="vm"))
    assert [(c.setting, c.effect) for c in changes] == [("wayland", Effect.UNSUPPORTED)]

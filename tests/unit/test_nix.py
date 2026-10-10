import json
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock

import pytest

from nixant.errors import NixantError, UsageError
from nixant.models import MachineSpec
from nixant.nix.build import build, gcroot_path
from nixant.nix.eval import evaluate, evaluate_spec
from nixant.nix.store import is_drv_path, is_store_path
from nixant.run import Runner

DRV = "/nix/store/" + "a" * 32 + "-system.drv"
SYSTEM = "/nix/store/" + "b" * 32 + "-system"


@pytest.fixture
def runtime() -> dict:
    return json.loads(
        (Path(__file__).parents[2] / "nix/tests/runtime.json").read_text()
    )


def runner_with(data: object) -> Mock:
    runner = Mock(spec=Runner)
    runner.run.side_effect = [
        subprocess.CompletedProcess([], 0, b"true\n"),
        subprocess.CompletedProcess([], 0, b""),
        subprocess.CompletedProcess([], 0, json.dumps(data).encode()),
    ]
    return runner


def test_frozen_runtime(runtime: dict) -> None:
    spec = MachineSpec.from_runtime(runtime)
    assert spec.user.uid == 1000
    assert spec.mounts[0].read_only is False
    with pytest.raises(FrozenInstanceError):
        spec.kind = "vm"


def test_gpu_runtime_roundtrip(runtime: dict) -> None:
    runtime["gpu"] = {"gid": 303}
    spec = MachineSpec.from_runtime(runtime)
    assert spec.gpu_gid == 303
    assert spec.to_runtime() == runtime


@pytest.mark.parametrize("version", [None, 2, True, "1"])
def test_schema_drift(runtime: dict, version: object) -> None:
    runtime["schemaVersion"] = version
    with pytest.raises(NixantError, match="matching nixant version"):
        MachineSpec.from_runtime(runtime)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("kind", "unknown"),
        ("instanceName", "-option"),
        ("cpus", True),
        ("memoryBytes", -1),
        ("workdir", "relative"),
        ("mounts", []),
        ("ports", {}),
        ("wayland", "yes"),
        ("gpu", True),
        ("gpu", {"gid": 0}),
    ],
)
def test_invalid_runtime(runtime: dict, key: str, value: object) -> None:
    runtime[key] = value
    with pytest.raises(NixantError, match="invalid nixant runtime"):
        MachineSpec.from_runtime(runtime)


def test_single_evaluation(
    runtime: dict, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = runner_with({"runtime": runtime, "drvPath": DRV})
    result = evaluate(tmp_path, "dev", runner)
    assert result.drv_path == DRV
    calls = [call for call in runner.run.call_args_list if call.args[0][0] == "nix"]
    assert len(calls) == 1
    assert "drvPath = c.system.build.toplevel.drvPath" in calls[0].args[0][-1]
    assert calls[0].kwargs == {"cwd": tmp_path, "capture": True}
    assert "evaluating dev" in capsys.readouterr().err


def test_runtime_only(runtime: dict, tmp_path: Path) -> None:
    runner = runner_with({"runtime": runtime})
    assert evaluate_spec(tmp_path, "dev", runner) == MachineSpec.from_runtime(runtime)
    assert "toplevel" not in runner.run.call_args.args[0][-1]


def test_missing_target(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="available targets: other"):
        evaluate(
            tmp_path, "dev", runner_with({"error": "target", "targets": ["other"]})
        )


def test_missing_module(tmp_path: Path) -> None:
    with pytest.raises(NixantError, match="nixant.nixosModules.container"):
        evaluate(tmp_path, "dev", runner_with({"error": "module"}))


def test_invalid_target_no_commands(tmp_path: Path) -> None:
    runner = Mock(spec=Runner)
    with pytest.raises(UsageError):
        evaluate(tmp_path, "../bad", runner)
    runner.run.assert_not_called()


def test_build_by_derivation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    runner = Mock(spec=Runner)
    runner.run.return_value = subprocess.CompletedProcess(
        [], 0, json.dumps([{"outputs": {"out": SYSTEM}}]).encode()
    )
    assert build(tmp_path, "dev", DRV, runner) == SYSTEM
    link = gcroot_path(tmp_path, "dev")
    assert link.parent.is_dir()
    runner.run.assert_called_once_with(
        ["nix", "build", DRV + "^out", "--out-link", str(link), "--json"],
        cwd=tmp_path,
        capture=True,
    )


def test_unrooted_build_leaves_the_gc_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    runner = Mock(spec=Runner)
    runner.run.return_value = subprocess.CompletedProcess(
        [], 0, json.dumps([{"outputs": {"out": SYSTEM}}]).encode()
    )
    assert build(tmp_path, "dev", DRV, runner, root_it=False) == SYSTEM
    assert not gcroot_path(tmp_path, "dev").parent.exists()
    runner.run.assert_called_once_with(
        ["nix", "build", DRV + "^out", "--no-link", "--json"],
        cwd=tmp_path,
        capture=True,
    )


def test_invalid_drv_no_build(tmp_path: Path) -> None:
    runner = Mock(spec=Runner)
    with pytest.raises(NixantError, match="invalid system derivation"):
        build(tmp_path, "dev", "--option", runner)
    runner.run.assert_not_called()


@pytest.mark.parametrize("response", [[], [{}], [{"outputs": {"out": "/tmp/unsafe"}}]])
def test_bad_build_response(
    response: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    runner = Mock(spec=Runner)
    runner.run.return_value = subprocess.CompletedProcess(
        [], 0, json.dumps(response).encode()
    )
    with pytest.raises(NixantError, match="invalid Nix build response"):
        build(tmp_path, "dev", DRV, runner)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("user", "name"), "root"),
        (("user", "name"), ""),
        (("user", "name"), "dev\0root"),
        (("user", "uid"), 0),
        (("user", "uid"), True),
        (("user", "gid"), -1),
        (("user", "home"), "relative"),
        (("user", "shell"), None),
        (("mounts", "workspace", "readOnly"), "false"),
        (("mounts", "workspace", "source"), ""),
    ],
)
def test_invalid_nested_runtime(runtime: dict, path: tuple, value: object) -> None:
    parent = runtime
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(NixantError, match="invalid nixant runtime"):
        MachineSpec.from_runtime(runtime)


@pytest.mark.parametrize("data", [None, [], "runtime"])
def test_runtime_requires_object(data: object) -> None:
    with pytest.raises(NixantError, match="expected an object"):
        MachineSpec.from_runtime(data)


@pytest.mark.parametrize("port", [0, -1, True, "80", 65536])
@pytest.mark.parametrize("field", ["host", "guest"])
def test_invalid_runtime_ports(runtime: dict, field: str, port: object) -> None:
    runtime["ports"] = [{"host": 8080, "guest": 80, "address": "127.0.0.1"}]
    runtime["ports"][0][field] = port
    with pytest.raises(NixantError, match="invalid nixant runtime"):
        MachineSpec.from_runtime(runtime)


def test_duplicate_runtime_mount_targets(runtime: dict) -> None:
    runtime["mounts"]["other"] = dict(runtime["mounts"]["workspace"])
    with pytest.raises(NixantError, match="mount targets must be unique"):
        MachineSpec.from_runtime(runtime)


def test_duplicate_runtime_host_ports(runtime: dict) -> None:
    runtime["ports"] = [
        {"host": 8080, "guest": guest, "address": "127.0.0.1"} for guest in (80, 81)
    ]
    with pytest.raises(NixantError, match="host ports must be unique"):
        MachineSpec.from_runtime(runtime)


def test_runtime_roundtrip_with_ports_and_readonly_mount(runtime: dict) -> None:
    runtime["ports"] = [
        {"host": 65535, "guest": 1, "address": "127.0.0.1", "hostname": None}
    ]
    runtime["mounts"]["workspace"]["readOnly"] = True
    spec = MachineSpec.from_runtime(runtime)
    assert spec.ports[0].host == 65535
    assert spec.ports[0].guest == 1
    assert spec.mounts[0].read_only is True
    assert spec.to_runtime() == runtime


@pytest.mark.parametrize("raw", [b"not JSON", b"[]", b"{}", b"null"])
def test_evaluate_rejects_malformed_response(tmp_path: Path, raw: bytes) -> None:
    runner = runner_with(None)
    runner.run.side_effect = [
        subprocess.CompletedProcess([], 0, b"true\n"),
        subprocess.CompletedProcess([], 0, b""),
        subprocess.CompletedProcess([], 0, raw),
    ]
    with pytest.raises(NixantError, match="invalid Nix evaluation response"):
        evaluate(tmp_path, "dev", runner)


@pytest.mark.parametrize("drv", [None, "/tmp/system.drv", "--option"])
def test_evaluate_rejects_invalid_derivation(
    runtime: dict, tmp_path: Path, drv: object
) -> None:
    with pytest.raises(NixantError, match="invalid system derivation path"):
        evaluate(tmp_path, "dev", runner_with({"runtime": runtime, "drvPath": drv}))


H = "a" * 32


@pytest.mark.parametrize(
    ("value", "store", "drv"),
    [
        (f"/nix/store/{H}-system", True, False),
        (f"/nix/store/{H}-system.drv", True, True),
        (f"/nix/store/{H}-a/b.drv", False, False),
        (f"/nix/store/{'A' * 32}-x.drv", False, False),
        ("/tmp/system.drv", False, False),
        (None, False, False),
    ],
)
def test_store_path_shapes(value: object, store: bool, drv: bool) -> None:
    assert is_store_path(value) is store
    assert is_drv_path(value) is drv


def test_runtime_port_hostname_roundtrip(runtime: dict) -> None:
    runtime["ports"] = [
        {"host": 8080, "guest": 80, "address": "127.0.0.1", "hostname": "a.localhost"},
        {"host": 8081, "guest": 81, "address": "127.0.0.1", "hostname": None},
    ]
    spec = MachineSpec.from_runtime(runtime)
    assert [p.hostname for p in spec.ports] == ["a.localhost", None]
    assert spec.to_runtime()["ports"] == runtime["ports"]


def test_duplicate_runtime_hostnames(runtime: dict) -> None:
    runtime["ports"] = [
        {"host": 8080 + n, "guest": 80 + n, "address": "127.0.0.1", "hostname": "a.b"}
        for n in range(2)
    ]
    with pytest.raises(NixantError, match="hostnames must be unique"):
        MachineSpec.from_runtime(runtime)

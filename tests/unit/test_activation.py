import io
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from nixant.errors import CommandError, NixantError
from nixant.models import MachineSpec, MachineState
from nixant.nix.activate import activate, can_skip
from nixant.ownership import PREFIX
from nixant.providers.base import Provider
from nixant.run import Runner

SYSTEM = "/nix/store/" + "a" * 32 + "-system"


def result(
    rc: int = 0, stdout: bytes = b"", stderr: bytes = b""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout, stderr)


@pytest.fixture
def spec() -> MachineSpec:
    return MachineSpec.from_runtime(
        json.loads((Path(__file__).parents[2] / "nix/tests/runtime.json").read_text())
    )


def setup(rc: int, stderr: bytes = b"") -> tuple[Mock, Mock, list]:
    provider = Mock(spec=Provider)
    runner = Mock(spec=Runner, verbose=False)
    events = []
    provider.set_metadata.side_effect = lambda name, values: events.append(values)
    runner.run.return_value = result(stdout=(SYSTEM + "\n").encode())
    provider.run.side_effect = [result(), result(), result(rc, stderr=stderr)]
    return provider, runner, events


@pytest.mark.parametrize(("rc", "expected"), [(0, "ok"), (4, "degraded")])
def test_completion(spec: MachineSpec, rc: int, expected: str) -> None:
    provider, runner, events = setup(rc)
    if rc == 4:
        provider.run.side_effect = [result(), result(), result(rc), result()]
    assert activate(provider, runner, spec, SYSTEM) == expected
    assert events[0] == {PREFIX + "activation": "pending"}
    assert events[-1] == {
        PREFIX + "activation": expected,
        PREFIX + "system": SYSTEM,
        PREFIX + "user": "dev",
        PREFIX + "workdir": "/workspace",
    }
    runner.pipe.assert_not_called()


@pytest.mark.parametrize("rc", [1, 2, 3, 5])
def test_failure(spec: MachineSpec, rc: int) -> None:
    provider, runner, events = setup(rc)
    with pytest.raises(NixantError, match=f"status {rc}"):
        activate(provider, runner, spec, SYSTEM)
    assert events == [
        {PREFIX + "activation": "pending"},
        {PREFIX + "activation": "failed"},
    ]


def test_guest_lock(spec: MachineSpec) -> None:
    provider, runner, events = setup(1, b"Could not acquire lock")
    with pytest.raises(
        NixantError, match="another activation is running inside test-dev"
    ):
        activate(provider, runner, spec, SYSTEM)
    assert events[-1] == {PREFIX + "activation": "failed"}


@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), subprocess.TimeoutExpired(["incus"], 1)]
)
def test_interrupt_pending(spec: MachineSpec, error: BaseException) -> None:
    provider, runner, events = setup(0)
    provider.run.side_effect = error
    with pytest.raises(NixantError, match="retry"):
        activate(provider, runner, spec, SYSTEM, timeout=1)
    assert events == [{PREFIX + "activation": "pending"}]


def test_transfer_pending_before_import(spec: MachineSpec) -> None:
    provider, runner, events = setup(0)

    @contextmanager
    def pipe(argv: list[str], *, timeout: float | None = None):
        assert events == [{PREFIX + "activation": "pending"}]
        assert argv == ["nix-store", "--export", SYSTEM]
        yield io.BytesIO(b"closure")

    runner.pipe.side_effect = pipe
    provider.run.side_effect = [
        result(stdout=SYSTEM.encode()),
        result(),
        result(),
        result(),
    ]
    assert activate(provider, runner, spec, SYSTEM) == "ok"
    assert provider.run.call_args_list[1].args[1] == ["nix-store", "--import"]


def test_import_failure(spec: MachineSpec) -> None:
    provider, runner, events = setup(0)
    runner.pipe.return_value.__enter__ = Mock(return_value=io.BytesIO())
    runner.pipe.return_value.__exit__ = Mock(return_value=False)
    provider.run.side_effect = [
        result(stdout=SYSTEM.encode()),
        CommandError(["incus"], 1),
    ]
    with pytest.raises(CommandError):
        activate(provider, runner, spec, SYSTEM)
    assert events[-1] == {PREFIX + "activation": "failed"}


@pytest.mark.parametrize(
    ("ready", "path", "expected"),
    [
        ("running", SYSTEM, "ok"),
        ("degraded", SYSTEM, "degraded"),
        ("running", "/nix/store/other", "failed"),
    ],
)
def test_reboot(
    spec: MachineSpec,
    ready: str,
    path: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, runner, events = setup(100)
    monkeypatch.setattr("nixant.nix.activate.wait_ready", Mock(return_value=ready))
    provider.run.side_effect = [
        result(),
        result(),
        result(100),
        result(stdout=path.encode()),
        result(),
    ]
    if expected == "failed":
        with pytest.raises(NixantError, match="did not boot"):
            activate(provider, runner, spec, SYSTEM)
    else:
        assert activate(provider, runner, spec, SYSTEM) == expected
    assert events[1] == {PREFIX + "activation": "reboot-required"}
    assert events[-1][PREFIX + "activation"] == expected
    provider.restart.assert_called_once_with("test-dev")


def test_restart_failure_retains_reboot_required(spec: MachineSpec) -> None:
    provider, runner, events = setup(100)
    provider.restart.side_effect = NixantError("restart failed")
    with pytest.raises(NixantError, match="restart failed"):
        activate(provider, runner, spec, SYSTEM)
    assert events[-1] == {PREFIX + "activation": "reboot-required"}


@pytest.mark.parametrize(
    "activation", ["pending", "failed", "degraded", "reboot-required", ""]
)
def test_skip_requires_ok(activation: str) -> None:
    state = MachineState(
        "dev",
        "Running",
        "container",
        {
            PREFIX + "activation": activation,
            PREFIX + "system": SYSTEM,
        },
        {},
    )
    provider = Mock(spec=Provider)
    assert not can_skip(provider, state, SYSTEM)
    provider.run.assert_not_called()


def test_skip_requires_all_three() -> None:
    state = MachineState(
        "dev",
        "Running",
        "container",
        {
            PREFIX + "activation": "ok",
            PREFIX + "system": SYSTEM,
        },
        {},
    )
    provider = Mock(spec=Provider)
    provider.run.return_value = result(stdout=SYSTEM.encode())
    assert can_skip(provider, state, SYSTEM)
    assert not can_skip(provider, state, "different")
    provider.run.return_value = result(stdout=b"different")
    assert not can_skip(provider, state, SYSTEM)

import io
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from nixant.errors import CommandError, NixantError
from nixant.incus import IncusProvider
from nixant.models import MachineSpec, MachineState
from nixant.nix.activate import activate, can_skip
from nixant.ownership import PREFIX
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
    provider = Mock(spec=IncusProvider)
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
    provider.restart.assert_called_once_with("test-dev", timeout=None)


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
    provider = Mock(spec=IncusProvider)
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
    provider = Mock(spec=IncusProvider)
    provider.run.return_value = result(stdout=SYSTEM.encode())
    assert can_skip(provider, state, SYSTEM)
    assert not can_skip(provider, state, "different")
    provider.run.return_value = result(stdout=b"different")
    assert not can_skip(provider, state, SYSTEM)


def test_untrusted_missing_paths_abort_before_export(spec: MachineSpec) -> None:
    provider, runner, events = setup(0)
    provider.run.side_effect = [result(stdout=b"/nix/store/unrelated-secret\n")]
    with pytest.raises(NixantError, match="outside the system closure"):
        activate(provider, runner, spec, SYSTEM)
    runner.pipe.assert_not_called()
    assert provider.run.call_count == 1  # Never set the profile or switch.
    assert events == [
        {PREFIX + "activation": "pending"},
        {PREFIX + "activation": "failed"},
    ]


@pytest.mark.parametrize("stage", ["check", "switch"])
def test_signal_termination_retains_pending(spec: MachineSpec, stage: str) -> None:
    provider, runner, events = setup(-15)
    if stage == "check":
        provider.run.side_effect = CommandError(["incus"], -15)
    with pytest.raises(NixantError, match="interrupted.*retry"):
        activate(provider, runner, spec, SYSTEM)
    assert events == [{PREFIX + "activation": "pending"}]


def test_activation_passes_remaining_budget_to_each_command(
    spec: MachineSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, runner, events = setup(0)
    monkeypatch.setattr(
        "nixant.nix.activate.time.monotonic",
        Mock(side_effect=[100, 100, 103, 105, 107]),
    )
    assert activate(provider, runner, spec, SYSTEM, timeout=10) == "ok"
    assert runner.run.call_args.kwargs["timeout"] == 10
    assert [call.kwargs["timeout"] for call in provider.run.call_args_list] == [7, 5, 3]
    assert events[-1][PREFIX + "activation"] == "ok"


def test_expired_budget_stops_before_next_guest_command(
    spec: MachineSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, runner, events = setup(0)
    monkeypatch.setattr(
        "nixant.nix.activate.time.monotonic", Mock(side_effect=[100, 100, 111])
    )
    with pytest.raises(NixantError, match="did not finish.*retry"):
        activate(provider, runner, spec, SYSTEM, timeout=10)
    provider.run.assert_not_called()
    assert events == [{PREFIX + "activation": "pending"}]


def test_reboot_recovery_cannot_report_success_after_deadline(
    spec: MachineSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, runner, events = setup(100)
    now = [0.0]
    monkeypatch.setattr("nixant.nix.activate.time.monotonic", lambda: now[0])

    def restart(*args, **kwargs):
        now[0] = 11.0

    provider.restart.side_effect = restart
    provider.run.side_effect = [
        result(),
        result(),
        result(100),
        result(stdout=SYSTEM.encode()),
    ]
    monkeypatch.setattr("nixant.nix.activate.wait_ready", Mock(return_value="running"))
    error = None
    try:
        activate(provider, runner, spec, SYSTEM, timeout=10)
    except (NixantError, subprocess.TimeoutExpired) as exc:
        error = exc
    assert error is not None, "activation succeeded after its deadline"
    assert events[-1][PREFIX + "activation"] not in ("ok", "degraded")


@pytest.mark.parametrize("stage", ["restart", "ready", "verify"])
def test_reboot_deadline_expiry_at_each_stage(
    spec: MachineSpec, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    provider, runner, events = setup(100)
    expired = subprocess.TimeoutExpired(["incus"], 1)
    provider.run.side_effect = [result(), result(), result(100), expired]
    if stage == "restart":
        provider.restart.side_effect = expired
    ready = (
        Mock(side_effect=expired) if stage == "ready" else Mock(return_value="running")
    )
    monkeypatch.setattr("nixant.nix.activate.wait_ready", ready)
    if stage == "ready":
        provider.run.side_effect = [result(), result(), result(100)]
    with pytest.raises(NixantError, match="did not finish within 10s.*retry"):
        activate(provider, runner, spec, SYSTEM, timeout=10)
    assert events[-1] == {PREFIX + "activation": "reboot-required"}


def test_reboot_recovery_shares_original_deadline(
    spec: MachineSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, runner, events = setup(100)
    now = [0.0]
    monkeypatch.setattr("nixant.nix.activate.time.monotonic", lambda: now[0])

    def advance(amount: float):
        def step(*args, **kwargs):
            now[0] += amount

        return step

    provider.restart.side_effect = advance(3)
    provider.run.side_effect = [
        result(),
        result(),
        result(100),
        result(stdout=SYSTEM.encode()),
    ]
    ready = Mock(side_effect=lambda *a, **k: advance(2)() or "running")
    monkeypatch.setattr("nixant.nix.activate.wait_ready", ready)
    assert activate(provider, runner, spec, SYSTEM, timeout=10) == "ok"
    assert provider.restart.call_args.kwargs["timeout"] == 10
    assert ready.call_args.kwargs["timeout"] == 7
    assert provider.run.call_args.kwargs["timeout"] == 5
    assert events[-1][PREFIX + "activation"] == "ok"

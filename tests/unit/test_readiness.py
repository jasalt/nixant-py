import subprocess
from unittest.mock import Mock

import pytest

from nixant.errors import NixantError
from nixant.incus import IncusProvider
from nixant.readiness import wait_ready


def result(
    rc: int, stdout: bytes = b"", stderr: bytes = b""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout, stderr)


def test_running() -> None:
    provider = Mock(spec=IncusProvider)
    provider.run.side_effect = [result(0), result(0, b"running\n")]
    assert wait_ready(provider, "dev", "container") == "running"
    assert 0 < provider.run.call_args.kwargs["timeout"] <= 60


def test_degraded_verbose() -> None:
    provider = Mock(spec=IncusProvider)
    provider.run.side_effect = [result(0), result(1, b"degraded\n"), result(0)]
    assert wait_ready(provider, "dev", "vm", verbose=True) == "degraded"
    assert provider.run.call_args.args[1] == ["systemctl", "--failed", "--no-pager"]
    assert 60 < provider.run.call_args.kwargs["timeout"] <= 180


def test_retry_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nixant.readiness.time.sleep", lambda _: None)
    provider = Mock(spec=IncusProvider)
    provider.run.side_effect = [
        result(1, stderr=b"VM agent isn't currently running"),
        result(0),
        result(0, b"running\n"),
    ]
    assert wait_ready(provider, "dev", "vm") == "running"


def test_timeout_names_last_state(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Mock(side_effect=[0, 0, 0, 60, 60])
    monkeypatch.setattr("nixant.readiness.time.monotonic", clock)
    provider = Mock(spec=IncusProvider)
    provider.run.side_effect = [result(0), result(1, b"maintenance\n")]
    with pytest.raises(
        NixantError, match="dev not ready within 60s; last state: maintenance"
    ):
        wait_ready(provider, "dev", "container")


def test_hung_exec_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nixant.readiness.time.monotonic", Mock(side_effect=[0, 0, 60, 60])
    )
    provider = Mock(spec=IncusProvider)
    provider.run.side_effect = subprocess.TimeoutExpired(["incus"], 5)
    with pytest.raises(NixantError, match="timed out"):
        wait_ready(provider, "dev", "container")
    assert provider.run.call_args.kwargs["timeout"] == 5

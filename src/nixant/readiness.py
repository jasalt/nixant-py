"""Bounded guest readiness after starts and restarts."""

import subprocess
import time

from nixant.errors import NixantError
from nixant.providers.base import Provider


def wait_ready(
    provider: Provider,
    name: str,
    kind: str,
    *,
    verbose: bool = False,
    timeout: float | None = None,
) -> str:
    duration = timeout if timeout is not None else (180 if kind == "vm" else 60)
    deadline = time.monotonic() + duration
    last = "exec unavailable"
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            probe = provider.run(
                name,
                ["true"],
                capture=True,
                capture_stderr=True,
                check=False,
                timeout=min(remaining, 5),
            )
            if probe.returncode == 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                state = provider.run(
                    name,
                    ["systemctl", "is-system-running", "--wait"],
                    capture=True,
                    capture_stderr=True,
                    check=False,
                    timeout=remaining,
                )
                last = state.stdout.decode(errors="replace").strip()
                if (last == "running" and state.returncode == 0) or (
                    last == "degraded" and state.returncode in (0, 1)
                ):
                    if last == "degraded" and verbose:
                        remaining = deadline - time.monotonic()
                        if remaining > 0:
                            provider.run(
                                name,
                                ["systemctl", "--failed", "--no-pager"],
                                check=False,
                                timeout=remaining,
                            )
                    return last
                last = (
                    last
                    or (state.stderr or b"unknown").decode(errors="replace").strip()
                )
            else:
                last = (
                    (probe.stderr or b"exec unavailable")
                    .decode(errors="replace")
                    .strip()
                )
        except subprocess.TimeoutExpired:
            last = "guest readiness command timed out"
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1, remaining))
    raise NixantError(
        f"instance {name} not ready within {duration}s; last state: {last}"
    )

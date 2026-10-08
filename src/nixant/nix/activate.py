"""Host-built closure transfer and recoverable activation state recording."""

import subprocess
import sys
import time

from nixant.errors import CommandError, NixantError
from nixant.incus import IncusProvider
from nixant.models import MachineSpec, MachineState
from nixant.ownership import PREFIX
from nixant.readiness import readiness_timeout, wait_ready
from nixant.run import Runner


def can_skip(provider: IncusProvider, state: MachineState, system: str) -> bool:
    if (
        state.config.get(PREFIX + "activation") != "ok"
        or state.config.get(PREFIX + "system") != system
    ):
        return False
    result = provider.run(
        state.name, ["readlink", "-f", "/run/current-system"], capture=True, check=False
    )
    return result.returncode == 0 and result.stdout.decode().strip() == system


def activate(
    provider: IncusProvider,
    runner: Runner,
    spec: MachineSpec,
    system: str,
    *,
    timeout: float | None = None,
) -> str:
    name = spec.instance_name
    deadline = None if timeout is None else time.monotonic() + timeout

    def remaining() -> float | None:
        if deadline is None:
            return None
        left = deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired(["activation"], timeout or 0)
        return left

    def record(state: str) -> None:
        provider.set_metadata(name, {PREFIX + "activation": state})

    def complete(state: str) -> str:
        provider.set_metadata(
            name,
            {
                PREFIX + "activation": state,
                PREFIX + "system": system,
                PREFIX + "user": spec.user.name,
                PREFIX + "workdir": spec.workdir,
            },
        )
        if state == "degraded":
            print(f"warning: {name} activated with failed units", file=sys.stderr)
            provider.run(name, ["systemctl", "--failed", "--no-pager"], check=False)
        return state

    record("pending")
    try:
        closure = runner.run(
            ["nix-store", "-qR", system], capture=True, timeout=remaining()
        )
        paths = closure.stdout.decode().splitlines()
        missing_result = provider.run(
            name,
            ["nix-store", "--check-validity", "--print-invalid", *paths],
            capture=True,
            timeout=remaining(),
        )
        missing = missing_result.stdout.decode().splitlines()
        if not set(missing).issubset(paths):
            raise NixantError("guest reported missing paths outside the system closure")
        if missing:
            print(f"copying {len(missing)} store paths to {name}…", file=sys.stderr)
            with runner.pipe(
                ["nix-store", "--export", *missing], timeout=remaining()
            ) as stream:
                provider.run(
                    name, ["nix-store", "--import"], stdin=stream, timeout=remaining()
                )
        provider.run(
            name,
            ["nix-env", "-p", "/nix/var/nix/profiles/system", "--set", system],
            timeout=remaining(),
        )
        switched = provider.run(
            name,
            [system + "/bin/switch-to-configuration", "switch"],
            check=False,
            tee_stderr=True,
            timeout=remaining(),
        )
    except (KeyboardInterrupt, subprocess.TimeoutExpired) as exc:
        # pending was written before any transfer; never overwrite it on interruption.
        if isinstance(exc, subprocess.TimeoutExpired):
            raise NixantError(
                f"activation did not finish within {timeout:g}s; aborted, run "
                "nixant up to retry (a guest nixpkgs older than 26.05 can hang "
                "here)"
            ) from exc
        raise NixantError("activation interrupted; run nixant up to retry") from exc
    except CommandError as exc:
        if exc.returncode < 0:
            raise NixantError("activation interrupted; run nixant up to retry") from exc
        record("failed")
        raise
    except NixantError:
        record("failed")
        raise
    if switched.returncode < 0:
        raise NixantError("activation interrupted; run nixant up to retry")
    if switched.returncode == 0:
        return complete("ok")
    if switched.returncode == 4:
        return complete("degraded")
    if switched.returncode == 100:
        record("reboot-required")
        print(f"new system needs a restart; restarting {name}", file=sys.stderr)
        try:
            # The original deadline covers recovery too; never restart the budget.
            # It only caps readiness, though: a guest that does not come back
            # fails after the usual boot allowance, not the whole deadline.
            provider.restart(name, timeout=remaining())
            left = remaining()
            boot = readiness_timeout(spec.kind)
            ready = wait_ready(
                provider,
                name,
                spec.kind,
                timeout=boot if left is None else min(boot, left),
            )
            current = provider.run(
                name,
                ["readlink", "-f", "/run/current-system"],
                capture=True,
                timeout=remaining(),
            )
        except (KeyboardInterrupt, subprocess.TimeoutExpired) as exc:
            if isinstance(exc, subprocess.TimeoutExpired):
                raise NixantError(
                    f"activation did not finish within {timeout}s while restarting "
                    f"{name}; run nixant up to retry"
                ) from exc
            raise NixantError("activation interrupted; run nixant up to retry") from exc
        if current.stdout.decode().strip() != system:
            record("failed")
            raise NixantError(f"instance {name} did not boot the new system")
        return complete("degraded" if ready == "degraded" else "ok")
    record("failed")
    if b"Could not acquire lock" in (switched.stderr or b""):
        raise NixantError(f"another activation is running inside {name}")
    raise NixantError(
        f"activation of {name} failed with status {switched.returncode}; "
        "fix the configuration and run nixant rebuild"
    )

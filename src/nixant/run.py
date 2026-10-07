"""Argv-only process execution; binary streams support Nix closure transport."""

import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

from nixant.errors import CommandError, NixantError


def check_host_tools() -> None:
    """Use host tools rather than wrapping clients from a different installation."""
    missing = [tool for tool in ("incus", "nix", "git") if shutil.which(tool) is None]
    if missing:
        raise NixantError(f"required tools not found on PATH: {', '.join(missing)}")


class Runner:
    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose

    def run(
        self,
        argv: Sequence[str],
        *,
        capture: bool = False,
        capture_stderr: bool = False,
        stdin: BinaryIO | None = None,
        cwd: Path | None = None,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Capture stdout optionally; stderr is inherited unless explicitly captured.

        Use check=False for commands whose statuses are part of their protocol.
        TimeoutExpired and KeyboardInterrupt deliberately propagate so activation
        can distinguish interruption from failure and retain pending metadata.
        """
        if isinstance(argv, (str, bytes)) or not argv:
            raise ValueError("expected a nonempty argv sequence, not a shell command")
        args = list(argv)
        if self.verbose:
            print(f"+ {shlex.join(args)}", file=sys.stderr, flush=True)
        try:
            result = subprocess.run(
                args,
                stdin=stdin,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture_stderr else None,
                cwd=cwd,
                timeout=timeout,
                check=False,
            )
        except OSError as exc:
            raise NixantError(f"could not run {shlex.join(args)}: {exc}") from exc
        if check and result.returncode:
            raise CommandError(args, result.returncode, result.stderr)
        return result

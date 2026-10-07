"""Argv-only process execution; binary streams support Nix closure transport."""

import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, cast

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
        tee_stderr: bool = False,
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
            if tee_stderr:
                if capture:
                    raise ValueError(
                        "tee_stderr cannot be combined with stdout capture"
                    )
                result = self._tee(args, stdin=stdin, cwd=cwd, timeout=timeout)
            else:
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

    @staticmethod
    def _tee(
        args: list[str],
        *,
        stdin: BinaryIO | None,
        cwd: Path | None,
        timeout: float | None,
    ) -> subprocess.CompletedProcess[bytes]:
        with subprocess.Popen(
            args, stdin=stdin, cwd=cwd, stderr=subprocess.PIPE
        ) as child:
            assert child.stderr is not None
            stream = child.stderr
            chunks: list[bytes] = []

            def forward() -> None:
                while chunk := os.read(stream.fileno(), 8192):
                    chunks.append(chunk)
                    sys.stderr.write(chunk.decode(errors="replace"))
                    sys.stderr.flush()

            thread = threading.Thread(target=forward, daemon=True)
            thread.start()
            try:
                code = child.wait(timeout=timeout)
            except BaseException:
                child.kill()
                child.wait()
                raise
            finally:
                thread.join()
            return subprocess.CompletedProcess(args, code, None, b"".join(chunks))

    @contextmanager
    def pipe(
        self, argv: Sequence[str], *, timeout: float | None = None
    ) -> Iterator[BinaryIO]:
        """Stream a producer into a consumer and reap it on every exit path."""
        if isinstance(argv, (str, bytes)) or not argv:
            raise ValueError("expected a nonempty argv sequence")
        if self.verbose:
            print(f"+ {shlex.join(argv)}", file=sys.stderr, flush=True)
        started = time.monotonic()
        try:
            child = subprocess.Popen(list(argv), stdout=subprocess.PIPE)
        except OSError as exc:
            raise NixantError(f"could not run {shlex.join(argv)}: {exc}") from exc
        assert child.stdout is not None
        try:
            yield cast(BinaryIO, child.stdout)
            child.stdout.close()
            remaining = (
                None
                if timeout is None
                else max(0, timeout - (time.monotonic() - started))
            )
            code = child.wait(timeout=remaining)
            if code:
                raise CommandError(argv, code)
        finally:
            child.stdout.close()
            if child.poll() is None:
                child.kill()
            child.wait()

"""Expected failures that can be reported without a traceback."""

import shlex
from collections.abc import Sequence


class NixantError(Exception):
    """An expected operational failure (exit 1)."""


class UsageError(NixantError):
    """Invalid user input (exit 2)."""


class CommandError(NixantError):
    """A failed child process, retaining its status for callers."""

    def __init__(
        self, argv: Sequence[str], returncode: int, stderr: bytes | None = None
    ) -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr
        message = f"command exited with status {returncode}: {shlex.join(argv)}"
        if stderr:
            message += "\n" + stderr.decode(errors="replace").rstrip()
        super().__init__(message)

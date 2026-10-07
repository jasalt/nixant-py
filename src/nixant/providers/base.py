"""The provider boundary; planning and ownership checks stay outside it."""

import subprocess
from collections.abc import Mapping
from typing import BinaryIO, Protocol

from nixant.models import MachineSpec, MachineState, MountSpec


class Provider(Protocol):
    def inspect(self, name: str) -> MachineState | None: ...
    def find(self, metadata: Mapping[str, str]) -> list[MachineState]: ...
    def create(self, spec: MachineSpec, metadata: Mapping[str, str]) -> None: ...
    def start(self, name: str) -> None: ...
    def stop(self, name: str, *, force: bool = False) -> None: ...
    def restart(self, name: str, *, force: bool = False) -> None: ...
    def destroy(self, name: str) -> None: ...
    def set_metadata(self, name: str, metadata: Mapping[str, str]) -> None: ...
    def ensure_mount(
        self, name: str, mount: MountSpec, *, verify: bool = False
    ) -> None: ...
    def run(
        self,
        name: str,
        argv: list[str],
        *,
        user: str | None = None,
        cwd: str | None = None,
        stdin: BinaryIO | None = None,
        capture: bool = False,
        capture_stderr: bool = False,
        tee_stderr: bool = False,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]: ...
    def exec_argv(
        self, name: str, argv: list[str], *, user: str, cwd: str
    ) -> list[str]: ...

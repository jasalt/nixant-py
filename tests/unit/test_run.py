import subprocess
import sys
from pathlib import Path

import pytest

from nixant.errors import CommandError, NixantError
from nixant.run import Runner, check_host_tools


def test_capture_leaves_stderr_visible(capfd: pytest.CaptureFixture[str]) -> None:
    result = Runner().run(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ],
        capture=True,
    )
    assert result.stdout == b"out\n"
    assert result.stderr is None
    assert capfd.readouterr().err == "err\n"


def test_stdin_and_literal_arguments(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.write_bytes(b"\x00closure\xff")
    with source.open("rb") as stream:
        result = Runner().run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ],
            stdin=stream,
            capture=True,
            cwd=tmp_path,
        )
    assert result.stdout == source.read_bytes()
    literal = "$(touch unwanted); *"
    result = Runner().run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", literal], capture=True
    )
    assert result.stdout == (literal + "\n").encode()


def test_failure_retains_status_and_stderr() -> None:
    argv = [sys.executable, "-c", "import sys; sys.stderr.write('broken'); sys.exit(4)"]
    with pytest.raises(CommandError) as error:
        Runner().run(argv, capture_stderr=True)
    assert error.value.returncode == 4
    assert error.value.argv == tuple(argv)
    assert "broken" in str(error.value)
    assert Runner().run(argv, check=False, capture_stderr=True).returncode == 4


def test_missing_executable() -> None:
    with pytest.raises(NixantError, match="could not run"):
        Runner().run(["/nonexistent/nixant-test"])


def test_timeout() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        Runner().run(
            [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.01
        )


def test_verbose_echo(capsys: pytest.CaptureFixture[str]) -> None:
    Runner(verbose=True).run([sys.executable, "-c", "pass", "two words"])
    assert "'two words'" in capsys.readouterr().err


@pytest.mark.parametrize("argv", ["echo unsafe", []])
def test_reject_shell_commands(argv: str | list[str]) -> None:
    with pytest.raises(ValueError):
        Runner().run(argv)


def test_tee_stderr(capfd: pytest.CaptureFixture[str]) -> None:
    result = Runner().run(
        [sys.executable, "-c", "import sys; print('progress', file=sys.stderr)"],
        tee_stderr=True,
    )
    assert result.stderr == b"progress\n"
    assert capfd.readouterr().err == "progress\n"


def test_pipe_binary_and_failure() -> None:
    with Runner().pipe(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'abc')"]
    ) as stream:
        assert stream.read() == b"abc"
    with (
        pytest.raises(CommandError),
        Runner().pipe([sys.executable, "-c", "raise SystemExit(3)"]) as stream,
    ):
        assert stream.read() == b""


def test_pipe_consumer_failure_terminates_producer() -> None:
    with (
        pytest.raises(RuntimeError),
        Runner().pipe([sys.executable, "-c", "import time; time.sleep(60)"]),
    ):
        raise RuntimeError("consumer failed")


def test_missing_host_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    with pytest.raises(NixantError, match="incus, nix, git"):
        check_host_tools()

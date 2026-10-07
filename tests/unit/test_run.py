import subprocess
import sys
from collections.abc import Iterator
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


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[subprocess.Popen]]:
    """Keep real children observable and reap them even if a test assertion fails."""
    processes = []
    real_popen = subprocess.Popen

    def track(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr("nixant.run.subprocess.Popen", track)
    yield processes
    for child in processes:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize("mode", ["tee", "pipe"])
def test_timeout_kills_and_reaps_child(
    children: list[subprocess.Popen], mode: str
) -> None:
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    with pytest.raises(subprocess.TimeoutExpired):
        if mode == "tee":
            Runner().run(argv, tee_stderr=True, timeout=0.02)
        else:
            with Runner().pipe(argv, timeout=0.02):
                pass
    assert len(children) == 1
    child = children[0]
    assert child.returncode is not None and child.returncode < 0
    stream = child.stderr if mode == "tee" else child.stdout
    assert stream is not None and stream.closed


@pytest.mark.parametrize(
    "error", [RuntimeError("consumer failed"), KeyboardInterrupt()]
)
def test_pipe_consumer_exception_reaps_child(
    children: list[subprocess.Popen], error: BaseException
) -> None:
    with (
        pytest.raises(type(error)),
        Runner().pipe([sys.executable, "-c", "import time; time.sleep(60)"]),
    ):
        raise error
    assert len(children) == 1
    assert children[0].returncode is not None and children[0].returncode < 0
    assert children[0].stdout.closed


def test_tee_interrupt_kills_and_reaps_child(
    children: list[subprocess.Popen], monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked_popen = subprocess.Popen

    def interrupt_on_wait(*args, **kwargs):
        child = tracked_popen(*args, **kwargs)
        real_wait = child.wait

        def interrupt(timeout=None):
            monkeypatch.setattr(child, "wait", real_wait)
            raise KeyboardInterrupt

        monkeypatch.setattr(child, "wait", interrupt)
        return child

    monkeypatch.setattr("nixant.run.subprocess.Popen", interrupt_on_wait)
    with pytest.raises(KeyboardInterrupt):
        Runner().run(
            [sys.executable, "-c", "import time; time.sleep(60)"], tee_stderr=True
        )
    assert children[0].returncode is not None and children[0].returncode < 0
    assert children[0].stderr.closed


def test_tee_rejects_stdout_capture_before_spawning(
    children: list[subprocess.Popen],
) -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        Runner().run([sys.executable, "-c", "pass"], tee_stderr=True, capture=True)
    assert children == []


def test_pipe_missing_executable() -> None:
    with (
        pytest.raises(NixantError, match="could not run"),
        Runner().pipe(["/nonexistent/nixant-test"]),
    ):
        pytest.fail("missing producer must not yield a stream")

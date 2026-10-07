import json
import subprocess
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from typer.testing import CliRunner

from nixant.cli import app
from nixant.errors import CommandError, NixantError, UsageError
from nixant.init import (
    init_project,
    input_url,
    list_templates,
    nix_string,
    pin_url,
    propose_instance_name,
    render,
)
from nixant.models import MachineState
from nixant.run import Runner

REPO = Path(__file__).parents[2]


@pytest.mark.parametrize(
    ("directory", "expected"),
    [
        ("shop", "shop-dev"),
        ("My_Shop.2", "my-shop-2-dev"),
        ("123", "ndev"),
        ("-9app-", "app-dev"),
        ("a" * 80, "a" * 59 + "-dev"),
    ],
)
def test_propose_instance_name(directory: str, expected: str) -> None:
    name = propose_instance_name(Path("/x") / directory)
    assert len(name) <= 63
    assert name == expected


def test_render_replaces_markers_and_escapes() -> None:
    text = 'a = "nixant-template-dev"; b = "nixant-template-url";'
    assert render(text, "x-dev", 'path:/a"b') == 'a = "x-dev"; b = "path:/a\\"b";'
    assert nix_string("${x}") == "\\${x}"


def test_input_url_prefers_canonical(tmp_path: Path) -> None:
    assert input_url({}, tmp_path) == f"path:{tmp_path}"
    assert input_url({"NIXANT_FLAKE_URL": "github:o/r"}, tmp_path) == "github:o/r"


def fake_runner(
    directory: Path, lock_fails: bool = False, *, in_git: bool = True
) -> Mock:
    runner = Mock(spec=Runner)

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if argv[:3] == ["nix", "eval", "--json"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"default": "d"}).encode()
            )
        if argv[:3] == ["nix", "flake", "init"]:
            (directory / "nix").mkdir()
            template = REPO / "nix/templates/default"
            (directory / "flake.nix").write_text((template / "flake.nix").read_text())
            (directory / "nix/dev.nix").write_text(
                (template / "nix/dev.nix").read_text()
            )
        if argv[:3] == ["nix", "flake", "lock"]:
            if lock_fails:
                raise CommandError(argv, 1)
            (directory / "flake.lock").write_text("{}")
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                argv, 0 if in_git else 128, b"true\n" if in_git else b""
            )
        return subprocess.CompletedProcess(argv, 0, b"")

    runner.run.side_effect = run
    return runner


def commands(runner: Mock) -> list[list[str]]:
    return [call.args[0] for call in runner.run.call_args_list]


def test_init_writes_rewrites_locks_and_stages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "Shop"
    project.mkdir()
    provider = Mock()
    provider.inspect.return_value = None
    runner = fake_runner(project)
    env = {"NIXANT_SELF": str(REPO)}
    init_project(project, "default", runner, provider, env)
    flake = (project / "flake.nix").read_text()
    assert 'nixant.instanceName = "shop-dev";' in flake
    assert f'nixant.url = "path:{REPO}";' in flake
    assert "nixant-template" not in flake
    assert ["git", "add", "--", "flake.nix", "nix/dev.nix"] in commands(runner)
    assert ["nix", "flake", "lock"] in commands(runner)
    assert ["git", "add", "--", "flake.lock"] in commands(runner)
    out = capsys.readouterr().out
    assert "wrote flake.nix nix/dev.nix (instanceName: shop-dev)" in out
    assert "next: nixant up" in out


def test_init_pins_revision_with_canonical_url(tmp_path: Path) -> None:
    runner = fake_runner(tmp_path)
    env = {
        "NIXANT_SELF": str(REPO),
        "NIXANT_FLAKE_URL": "github:o/r",
        "NIXANT_REV": "abc",
    }
    init_project(
        tmp_path, "default", runner, Mock(inspect=Mock(return_value=None)), env
    )
    assert [
        "nix",
        "flake",
        "lock",
        "--override-input",
        "nixant",
        "github:o/r/abc",
    ] in commands(runner)
    assert 'nixant.url = "github:o/r";' in (tmp_path / "flake.nix").read_text()


def test_init_without_revision_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = fake_runner(tmp_path)
    env = {"NIXANT_SELF": str(REPO), "NIXANT_FLAKE_URL": "github:o/r"}
    init_project(
        tmp_path, "default", runner, Mock(inspect=Mock(return_value=None)), env
    )
    assert ["nix", "flake", "lock"] in commands(runner)
    assert "not to this CLI" in capsys.readouterr().err


def test_init_offline_prints_lock_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = fake_runner(tmp_path, lock_fails=True)
    init_project(
        tmp_path,
        "default",
        runner,
        Mock(inspect=Mock(return_value=None)),
        {"NIXANT_SELF": str(REPO)},
    )
    assert (tmp_path / "flake.nix").exists()
    assert "later run: nix flake lock" in capsys.readouterr().err
    assert ["git", "add", "--", "flake.lock"] not in commands(runner)


def test_init_warns_when_instance_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = Mock()
    provider.inspect.return_value = MachineState("x", "Running", "container", {}, {})
    init_project(
        tmp_path, "default", fake_runner(tmp_path), provider, {"NIXANT_SELF": str(REPO)}
    )
    assert "already exists" in capsys.readouterr().err
    provider.inspect.assert_called_once_with(
        f"{tmp_path.name.lower()}-dev".replace("_", "-").replace(".", "-")
    )


def test_init_existing_flake_prints_snippet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "flake.nix").write_text("{}")
    runner = fake_runner(tmp_path)
    init_project(tmp_path, "default", runner, Mock(), {"NIXANT_SELF": str(REPO)})
    assert (tmp_path / "flake.nix").read_text() == "{}"
    assert not any(c[:3] == ["nix", "flake", "init"] for c in commands(runner))
    out = capsys.readouterr().out
    assert "nixant.nixosModules.container" in out
    assert f"{tmp_path.name.lower()}-dev".replace("_", "-").replace(".", "-") in out
    assert "nixant-template" not in out


def test_init_unknown_template_lists_available(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="available: default"):
        init_project(
            tmp_path, "nope", fake_runner(tmp_path), Mock(), {"NIXANT_SELF": str(REPO)}
        )
    assert not (tmp_path / "flake.nix").exists()


def test_init_requires_self(tmp_path: Path) -> None:
    with pytest.raises(NixantError, match="NIXANT_SELF"):
        init_project(tmp_path, "default", Mock(), Mock(), {})


def test_init_list_cli_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Mock(spec=Runner)
    runner.run.return_value = subprocess.CompletedProcess(
        [], 0, json.dumps({"python": "Python tools", "default": "Minimal"}).encode()
    )
    provider = Mock()
    monkeypatch.setattr("nixant.cli.check_host_tools", lambda: None)
    monkeypatch.setattr("nixant.cli.Runner", Mock(return_value=runner))
    monkeypatch.setattr("nixant.cli.IncusProvider", provider)
    monkeypatch.setenv("NIXANT_SELF", str(REPO))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["init", "--list"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["default  Minimal", "python   Python tools"]
    runner.run.assert_called_once_with(
        [
            "nix",
            "eval",
            "--json",
            f"path:{REPO}#templates",
            "--apply",
            'ts: builtins.mapAttrs (_: t: t.description or "") ts',
        ],
        capture=True,
    )
    provider.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("incus_unavailable", [False, True])
def test_init_outside_git_locks_without_staging(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], incus_unavailable: bool
) -> None:
    runner = fake_runner(tmp_path, in_git=False)
    provider = Mock()
    provider.inspect.return_value = None
    if incus_unavailable:
        provider.inspect.side_effect = NixantError("Incus unavailable")
    init_project(tmp_path, "default", runner, provider, {"NIXANT_SELF": str(REPO)})
    assert (tmp_path / "flake.lock").is_file()
    assert "nixant-template" not in (tmp_path / "flake.nix").read_text()
    assert not any(argv[:2] == ["git", "add"] for argv in commands(runner))
    output = capsys.readouterr()
    assert "outside a git work tree" in output.err
    assert "next: nixant up" in output.out
    assert "added to git" not in output.out


@pytest.mark.parametrize("raw", [b"not JSON", b"[]", b"null"])
def test_invalid_template_listing(raw: bytes) -> None:
    runner = Mock(spec=Runner)
    runner.run.return_value = subprocess.CompletedProcess([], 0, raw)
    with pytest.raises(NixantError, match="invalid template listing"):
        list_templates(REPO, runner)


@pytest.mark.parametrize("query", ["", "?ref=main&dir=flake"])
def test_init_pins_git_url_without_changing_repository(
    tmp_path: Path, query: str
) -> None:
    runner = fake_runner(tmp_path)
    url = "git+https://example.invalid/team/nixant.git" + query
    revision = "a" * 40
    init_project(
        tmp_path,
        "default",
        runner,
        Mock(inspect=Mock(return_value=None)),
        {"NIXANT_SELF": str(REPO), "NIXANT_FLAKE_URL": url, "NIXANT_REV": revision},
    )
    lock = next(
        argv for argv in commands(runner) if argv[:3] == ["nix", "flake", "lock"]
    )
    pinned = urlsplit(lock[lock.index("--override-input") + 2])
    original = urlsplit(url)
    assert (pinned.scheme, pinned.netloc, pinned.path) == (
        original.scheme,
        original.netloc,
        original.path,
    )
    parameters = parse_qs(pinned.query)
    assert parameters.pop("rev", None) == [revision]
    assert parameters == parse_qs(original.query)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("github:o/r", "github:o/r/" + "a" * 40),
        ("github:o/r/main", "github:o/r/" + "a" * 40),
        ("github:o/r?dir=flake", "github:o/r/" + "a" * 40 + "?dir=flake"),
        (
            "git+https://h/r.git?rev=old&ref=main",
            "git+https://h/r.git?ref=main&rev=" + "a" * 40,
        ),
    ],
)
def test_pin_url(url: str, expected: str) -> None:
    assert pin_url(url, "a" * 40) == expected


@pytest.mark.parametrize("url", ["path:/x", "https://h/x.tar.gz", "github:owner"])
def test_unsupported_url_fails_before_writing(tmp_path: Path, url: str) -> None:
    env = {"NIXANT_SELF": str(REPO), "NIXANT_FLAKE_URL": url, "NIXANT_REV": "a" * 40}
    with pytest.raises(UsageError, match="cannot pin"):
        init_project(tmp_path, "default", fake_runner(tmp_path), Mock(), env)
    assert list(tmp_path.iterdir()) == []

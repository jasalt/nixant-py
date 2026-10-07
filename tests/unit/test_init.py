import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from nixant.errors import CommandError, NixantError, UsageError
from nixant.init import (
    init_project,
    input_url,
    nix_string,
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


def fake_runner(directory: Path, lock_fails: bool = False) -> Mock:
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
            return subprocess.CompletedProcess(argv, 0, b"true\n")
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

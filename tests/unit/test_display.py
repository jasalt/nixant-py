import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nixant.cli import app
from nixant.models import MachineSpec, MachineState
from nixant.nix.build import gcroot_path
from nixant.ownership import PREFIX, metadata


@pytest.fixture
def display(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Mock, Mock, Path]:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("nixant.cli.check_host_tools", lambda: None)
    monkeypatch.setattr("nixant.cli.discover_project", lambda: tmp_path)
    provider = Mock()
    provider.find.return_value = []
    monkeypatch.setattr("nixant.cli.IncusProvider", lambda _: provider)
    evaluate = Mock()
    monkeypatch.setattr("nixant.deploy.evaluate", evaluate)
    monkeypatch.setattr("nixant.cli.evaluate_spec", evaluate)
    return provider, evaluate, tmp_path


def test_status_no_evaluation(display: tuple) -> None:
    provider, evaluate, root = display
    config = {
        **metadata(root, "dev"),
        PREFIX + "schema": "2",
        PREFIX + "system": "/nix/store/system",
        PREFIX + "activation": "degraded",
    }
    provider.find.return_value = [
        MachineState("test-dev", "Running", "container", config, {}, ("10.0.0.2",))
    ]
    link = gcroot_path(root, "dev")
    link.parent.mkdir(parents=True)
    link.symlink_to("/nix/store/system")
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "RUNNING  container  10.0.0.2" in result.output
    assert "degraded, current; schema mismatch" in result.output
    assert str(root) in result.output
    evaluate.assert_not_called()


def test_status_absent(display: tuple) -> None:
    result = CliRunner().invoke(app, ["status", "dev"])
    assert result.exit_code == 0
    assert "dev: not created" in result.output
    display[1].assert_not_called()


@pytest.mark.parametrize("target", [[], ["dev"]])
def test_status_proxy_routes_and_host_conflicts(display: tuple, target: list) -> None:
    provider, evaluate, root = display
    state = MachineState(
        "test-dev",
        "Running",
        "container",
        {
            **metadata(root, "dev"),
            PREFIX + "routes": json.dumps(
                {
                    "site.localhost": 8101,
                    "mail.site.localhost": 9101,
                }
            ),
        },
        {},
    )
    other = MachineState(
        "other-project",
        "Running",
        "container",
        {PREFIX + "routes": '{"site.localhost": 8102}'},
        {},
    )
    provider.find.side_effect = [[state], [state, other]]
    result = CliRunner().invoke(app, ["status", *target])
    assert result.exit_code == 0, result.output
    assert "proxy routes (configured; reachability not checked):" in result.output
    assert "mail.site.localhost → 127.0.0.1:9101\n" in result.output
    assert "site.localhost → 127.0.0.1:8101 [conflict; not routed]" in result.output
    assert "other-project" not in result.output
    assert provider.find.call_args.args == ({PREFIX + "managed": "true"},)
    evaluate.assert_not_called()


@pytest.mark.parametrize(
    "status,routes",
    [
        ("Stopped", '{"site.localhost": 8101}'),
        ("Frozen", '{"site.localhost": 8101}'),
        ("Running", "invalid"),
        ("Running", "[]"),
        ("Running", '{"site.localhost": true}'),
        ("Running", "{}"),
    ],
)
@pytest.mark.parametrize("json_output", [False, True])
def test_status_ignores_ineligible_routes(
    display: tuple, status: str, routes: str, json_output: bool
) -> None:
    provider, evaluate, root = display
    provider.find.return_value = [
        MachineState(
            "test-dev",
            status,
            "container",
            {**metadata(root, "dev"), PREFIX + "routes": routes},
            {},
        )
    ]
    result = CliRunner().invoke(app, ["status", *(["--json"] if json_output else [])])
    assert result.exit_code == 0, result.output
    if json_output:
        record = json.loads(result.stdout)["instances"][0]
        assert record["proxy_routes"] == []
        assert record["build_status"] == "unknown"
        assert record["system"] == record["activation"] == "none"
        assert record["schema_mismatch"] is False
    else:
        assert "proxy routes" not in result.output
    provider.find.assert_called_once()
    evaluate.assert_not_called()


@pytest.mark.parametrize("args", [[], ["dev"], ["--orphans"]])
def test_status_json_empty(display: tuple, args: list) -> None:
    result = CliRunner().invoke(app, ["status", *args, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"version": 1, "instances": []}
    display[1].assert_not_called()


def test_status_json_record(display: tuple) -> None:
    provider, evaluate, root = display
    state = MachineState(
        "test-dev",
        "Running",
        "container",
        {
            **metadata(root, "dev"),
            PREFIX + "schema": "old",
            PREFIX + "system": "/nix/store/system",
            PREFIX + "activation": "degraded",
            PREFIX + "routes": '{"site.localhost": 8101, "mail.localhost": 9101}',
        },
        {},
        ("10.0.0.2",),
    )
    other = MachineState(
        "other",
        "Running",
        "container",
        {PREFIX + "routes": '{"site.localhost": 8102}'},
        {},
    )
    provider.find.side_effect = [[state], [state, other]]
    link = gcroot_path(root, "dev")
    link.parent.mkdir(parents=True)
    link.symlink_to("/nix/store/system")
    result = CliRunner().invoke(app, ["status", "dev", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "version": 1,
        "instances": [
            {
                "name": "test-dev",
                "target": "dev",
                "status": "Running",
                "kind": "container",
                "ephemeral": False,
                "ipv4": ["10.0.0.2"],
                "root": str(root),
                "override": False,
                "system": "/nix/store/system",
                "activation": "degraded",
                "build_status": "current",
                "schema": "old",
                "schema_mismatch": True,
                "proxy_routes": [
                    {
                        "hostname": "mail.localhost",
                        "upstream_address": "127.0.0.1",
                        "upstream_port": 9101,
                        "conflict": False,
                    },
                    {
                        "hostname": "site.localhost",
                        "upstream_address": "127.0.0.1",
                        "upstream_port": 8101,
                        "conflict": True,
                    },
                ],
            }
        ],
    }
    evaluate.assert_not_called()


def test_status_json_orphans(display: tuple) -> None:
    provider, evaluate, root = display
    provider.find.return_value = [
        MachineState(
            "gone", "Stopped", "container", metadata(root / "gone", "dev"), {}
        ),
        MachineState("here", "Running", "container", metadata(root, "dev"), {}),
    ]
    result = CliRunner().invoke(app, ["status", "--orphans", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "version": 1,
        "instances": [
            {
                "name": "gone",
                "status": "Stopped",
                "target": "dev",
                "root": str(root / "gone"),
            }
        ],
    }
    assert (
        CliRunner().invoke(app, ["status", "dev", "--orphans", "--json"]).exit_code == 2
    )
    evaluate.assert_not_called()


def test_config_roundtrip(display: tuple) -> None:
    provider, evaluate, root = display
    runtime = json.loads(
        (Path(__file__).parents[2] / "nix/tests/runtime.json").read_text()
    )
    spec = MachineSpec.from_runtime(runtime)
    assert spec.to_runtime() == runtime
    evaluate.return_value = spec
    result = CliRunner().invoke(app, ["config"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["runtime"] == runtime
    assert data["mountSources"]["workspace"] == str(root)
    assert data["instanceNameSource"] == "config"
    provider.find.assert_not_called()

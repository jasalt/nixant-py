import json
from pathlib import Path

from typer.testing import CliRunner

from nixant.cli import app
from nixant.models import MachineState
from nixant.ownership import PREFIX
from nixant.proxy import caddy_config, collect_routes


def _state(name: str, routes: object, status: str = "Running") -> MachineState:
    config = {} if routes is None else {PREFIX + "routes": json.dumps(routes)}
    return MachineState(name, status, "container", config, {})


def test_collect_routes_uses_running_instances_only() -> None:
    routes, warnings = collect_routes(
        [
            _state("a", {"a.localhost": 8001}),
            _state("b", {"b.localhost": 8002}, status="Stopped"),
            _state("c", None),
        ]
    )
    assert routes == {"a.localhost": 8001}
    assert warnings == []


def test_collect_routes_refuses_a_name_claimed_twice() -> None:
    routes, warnings = collect_routes(
        [
            _state("a", {"x.localhost": 1, "a.localhost": 2}),
            _state("b", {"x.localhost": 3}),
        ]
    )
    assert routes == {"a.localhost": 2}
    assert len(warnings) == 1
    assert "x.localhost" in warnings[0]
    assert "a and b" in warnings[0]


def test_collect_routes_ignores_malformed_metadata() -> None:
    bad = MachineState("d", "Running", "container", {PREFIX + "routes": "{"}, {})
    assert collect_routes([bad, _state("e", ["x"]), _state("f", {"y": "1"})]) == (
        {},
        [],
    )


def test_caddy_config_is_loopback_only_with_a_private_admin_socket() -> None:
    config = caddy_config(
        {"a.localhost": 8001}, Path("/run/user/1/s.sock"), ["127.0.0.1:80", "[::1]:80"]
    )
    assert config["admin"] == {"listen": "unix//run/user/1/s.sock"}
    server = config["apps"]["http"]["servers"]["nixant"]
    assert server["listen"] == ["127.0.0.1:80", "[::1]:80"]
    first, last = server["routes"][0], server["routes"][-1]
    assert first["match"] == [{"host": ["a.localhost"]}]
    assert first["handle"][0]["upstreams"] == [{"dial": "127.0.0.1:8001"}]
    # Unmatched hosts get a 404 rather than reaching an instance.
    assert "match" not in last
    assert last["handle"][0]["status_code"] == 404


def test_proxy_print_setup() -> None:
    result = CliRunner().invoke(app, ["proxy", "--print-setup", "--port", "80"])
    assert result.exit_code == 0
    assert "ip_unprivileged_port_start=80" in result.output
    assert "CAP_NET_BIND_SERVICE" in result.output

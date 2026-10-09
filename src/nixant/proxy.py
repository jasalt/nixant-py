"""Host-side Caddy that routes <name>.localhost to instances' loopback forwards."""

import http.client
import json
import os
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from nixant.errors import NixantError
from nixant.incus import IncusProvider
from nixant.models import MachineState
from nixant.ownership import PREFIX

POLL_SECONDS = 30
COMMAND_POLL_SECONDS = 5
SETTLE_SECONDS = 0.3


def collect_routes(states: list[MachineState]) -> tuple[dict[str, int], list[str]]:
    """Hostname -> host port of running instances, and warnings for refused names."""
    claims: dict[str, list[tuple[str, int]]] = {}
    for state in states:
        if state.status != "Running":
            continue
        try:
            recorded = json.loads(state.config.get(PREFIX + "routes", "{}"))
        except ValueError:
            recorded = None
        if not isinstance(recorded, dict):
            continue
        for hostname, port in recorded.items():
            if isinstance(hostname, str) and type(port) is int:
                claims.setdefault(hostname, []).append((state.name, port))
    routes: dict[str, int] = {}
    warnings: list[str] = []
    for hostname, owners in sorted(claims.items()):
        if len(owners) > 1:
            names = " and ".join(sorted(name for name, _ in owners))
            warnings.append(f"{hostname} is claimed by {names}; not routing it")
        else:
            routes[hostname] = owners[0][1]
    return routes, warnings


def local_routes(provider: IncusProvider) -> dict[str, int]:
    routes, warnings = collect_routes(provider.find({PREFIX + "managed": "true"}))
    for warning in warnings:
        print(f"nixant proxy: {warning}", file=sys.stderr, flush=True)
    return routes


def command_routes(argv: list[str]) -> dict[str, int]:
    """Routes printed by another host's `nixant proxy --routes`, e.g. in a VM."""
    try:
        result = subprocess.run(argv, capture_output=True, check=True, timeout=60)
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise NixantError(
            f"could not read routes from {shlex.join(argv)}: {exc}"
        ) from exc
    if not isinstance(data, dict) or not all(
        isinstance(name, str) and type(port) is int for name, port in data.items()
    ):
        raise NixantError("route export must be a JSON object of hostname to port")
    return data


def listen_addresses(port: int, https_port: int | None) -> list[str]:
    ports = [port] if https_port is None else [port, https_port]
    return [f"{host}:{p}" for p in ports for host in ("127.0.0.1", "[::1]")]


def caddy_config(
    routes: Mapping[str, int],
    admin_socket: Path,
    listen: list[str],
    *,
    http_port: int = 80,
    https_port: int | None = None,
) -> dict[str, Any]:
    handlers: list[dict[str, Any]] = [
        {
            "match": [{"host": [hostname]}],
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": f"127.0.0.1:{port}"}],
                }
            ],
        }
        for hostname, port in sorted(routes.items())
    ]
    # Anything else, including a rebinding attacker's name, reaches no instance.
    handlers.append({"handle": [{"handler": "static_response", "status_code": 404}]})
    server: dict[str, Any] = {"listen": listen, "routes": handlers}
    apps: dict[str, Any] = {
        "http": {
            "http_port": http_port,
            "https_port": https_port or 443,
            "servers": {"nixant": server},
        }
    }
    if https_port is None:
        server["automatic_https"] = {"disable": True}
    elif routes:
        # Caddy's own CA, not ACME: these names only exist on this machine.
        apps["tls"] = {
            "automation": {
                "policies": [
                    {
                        "subjects": sorted(routes),
                        "issuers": [{"module": "internal"}],
                    }
                ]
            }
        }
    return {"admin": {"listen": f"unix/{admin_socket}"}, "apps": apps}


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path) -> None:
        super().__init__("localhost")
        self.path = str(path)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.path)


def load(admin_socket: Path, config: Mapping[str, Any]) -> None:
    connection = _UnixConnection(admin_socket)
    try:
        connection.request(
            "POST",
            "/load",
            json.dumps(config),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read().decode(errors="replace")
    except OSError as exc:
        raise NixantError(f"could not reach caddy's admin socket: {exc}") from exc
    finally:
        connection.close()
    if response.status != 200:
        raise NixantError(f"caddy refused the routes: {body.strip()}")


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        raise NixantError("XDG_RUNTIME_DIR is not set; it holds caddy's admin socket")
    return Path(base)


def setup_help(port: int, https_port: int | None = None) -> str:
    https = (
        f"""
HTTPS uses Caddy's own certificate authority. Trust it once, so browsers accept
https://<name>.localhost (this installs the CA into your trust store):
  caddy trust
Port {https_port} needs the same permission as port {port}.
"""
        if https_port is not None
        else ""
    )
    https_flag = "" if https_port is None else f" --https --https-port {https_port}"
    return f"""\
Listening on port {port} needs one host change; nixant never makes it for you.

Either allow every program to bind ports from {port} up (simplest):
  sudo sysctl net.ipv4.ip_unprivileged_port_start={port}
  (persist it in /etc/sysctl.d/99-nixant.conf)

Or run the proxy as a system service that may bind low ports only itself:
  # /etc/systemd/system/nixant-proxy.service
  [Service]
  User={os.environ.get("USER", "YOU")}
  Environment=XDG_RUNTIME_DIR=/run/user/{os.getuid()}
  ExecStart={shutil.which("nixant") or "nixant"} proxy --port {port}{https_flag}
  AmbientCapabilities=CAP_NET_BIND_SERVICE
  Restart=on-failure
  [Install]
  WantedBy=multi-user.target
{https}"""


def serve(
    routes: Callable[[], dict[str, int]],
    *,
    port: int = 80,
    https_port: int | None = None,
    watch_incus: bool = True,
) -> None:
    caddy = shutil.which("caddy")
    if caddy is None:
        raise NixantError("caddy not found on PATH; install it or use nix shell")
    admin = runtime_dir() / "nixant-caddy.sock"
    listen = listen_addresses(port, https_port)

    def current() -> dict[str, Any]:
        return caddy_config(
            routes(), admin, listen, http_port=port, https_port=https_port
        )

    admin.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="nixant-proxy-") as scratch:
        initial = Path(scratch) / "caddy.json"
        config = current()
        initial.write_text(json.dumps(config))
        caddy_proc = subprocess.Popen([caddy, "run", "--config", str(initial)])
        monitor = (
            subprocess.Popen(
                ["incus", "monitor", "local:", "--type=lifecycle", "--format=json"],
                stdout=subprocess.PIPE,
            )
            if watch_incus
            else None
        )
        try:
            _watch(caddy_proc, monitor, current, admin, config)
        finally:
            for child in (monitor, caddy_proc):
                if child is not None and child.poll() is None:
                    child.terminate()
            for child in (monitor, caddy_proc):
                if child is not None:
                    child.wait()
            admin.unlink(missing_ok=True)


def _watch(
    caddy: "subprocess.Popen[bytes]",
    monitor: "subprocess.Popen[bytes] | None",
    current: Any,
    admin: Path,
    loaded: dict[str, Any],
) -> None:
    stream = monitor.stdout if monitor is not None else None
    last_poll = time.monotonic()
    while True:
        if caddy.poll() is not None:
            raise NixantError(
                f"caddy exited with status {caddy.returncode}; "
                "if it could not bind the port, see nixant proxy --print-setup"
            )
        if monitor is not None and monitor.poll() is not None:
            raise NixantError("incus monitor exited; is the Incus daemon running?")
        if stream is None:
            time.sleep(1.0)
            ready: list[Any] = []
        else:
            ready, _, _ = select.select([stream], [], [], 1.0)
        changed = False
        if ready:
            assert stream is not None
            os.read(stream.fileno(), 65536)
            # Let a burst of events (create, config, start) settle into one reload.
            while select.select([stream], [], [], SETTLE_SECONDS)[0]:
                os.read(stream.fileno(), 65536)
            changed = True
        elif time.monotonic() - last_poll >= (
            POLL_SECONDS if stream is not None else COMMAND_POLL_SECONDS
        ):
            changed = True
        if changed:
            last_poll = time.monotonic()
            config = current()
            if config != loaded:
                load(admin, config)
                loaded = config
                print("nixant proxy: routes reloaded", file=sys.stderr, flush=True)

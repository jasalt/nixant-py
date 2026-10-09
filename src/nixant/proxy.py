"""Host-side Caddy that routes <name>.localhost to instances' loopback forwards."""

import http.client
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nixant.errors import NixantError
from nixant.incus import IncusProvider
from nixant.models import MachineState
from nixant.ownership import PREFIX

POLL_SECONDS = 30
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


def caddy_config(
    routes: Mapping[str, int], admin_socket: Path, listen: list[str]
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
    return {
        "admin": {"listen": f"unix/{admin_socket}"},
        "apps": {
            "http": {
                "servers": {
                    "nixant": {
                        "listen": listen,
                        "routes": handlers,
                        "automatic_https": {"disable": True},
                    }
                }
            }
        },
    }


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


def setup_help(port: int) -> str:
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
  ExecStart={shutil.which("nixant") or "nixant"} proxy --port {port}
  AmbientCapabilities=CAP_NET_BIND_SERVICE
  Restart=on-failure
  [Install]
  WantedBy=multi-user.target
"""


def serve(provider: IncusProvider, *, port: int = 80) -> None:
    caddy = shutil.which("caddy")
    if caddy is None:
        raise NixantError("caddy not found on PATH; install it or use nix shell")
    admin = runtime_dir() / "nixant-caddy.sock"
    listen = [f"127.0.0.1:{port}", f"[::1]:{port}"]

    def current() -> dict[str, Any]:
        routes, warnings = collect_routes(provider.find({PREFIX + "managed": "true"}))
        for warning in warnings:
            print(f"nixant proxy: {warning}", file=sys.stderr, flush=True)
        return caddy_config(routes, admin, listen)

    admin.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="nixant-proxy-") as scratch:
        initial = Path(scratch) / "caddy.json"
        config = current()
        initial.write_text(json.dumps(config))
        caddy_proc = subprocess.Popen([caddy, "run", "--config", str(initial)])
        monitor = subprocess.Popen(
            ["incus", "monitor", "local:", "--type=lifecycle", "--format=json"],
            stdout=subprocess.PIPE,
        )
        try:
            _watch(caddy_proc, monitor, current, admin, config)
        finally:
            for child in (monitor, caddy_proc):
                if child.poll() is None:
                    child.terminate()
            for child in (monitor, caddy_proc):
                child.wait()
            admin.unlink(missing_ok=True)


def _watch(
    caddy: "subprocess.Popen[bytes]",
    monitor: "subprocess.Popen[bytes]",
    current: Any,
    admin: Path,
    loaded: dict[str, Any],
) -> None:
    assert monitor.stdout is not None
    stream = monitor.stdout
    last_poll = time.monotonic()
    while True:
        if caddy.poll() is not None:
            raise NixantError(
                f"caddy exited with status {caddy.returncode}; "
                "if it could not bind the port, see nixant proxy --print-setup"
            )
        if monitor.poll() is not None:
            raise NixantError("incus monitor exited; is the Incus daemon running?")
        ready, _, _ = select.select([stream], [], [], 1.0)
        changed = False
        if ready:
            os.read(stream.fileno(), 65536)
            # Let a burst of events (create, config, start) settle into one reload.
            while select.select([stream], [], [], SETTLE_SECONDS)[0]:
                os.read(stream.fileno(), 65536)
            changed = True
        elif time.monotonic() - last_poll >= POLL_SECONDS:
            changed = True
        if changed:
            last_poll = time.monotonic()
            config = current()
            if config != loaded:
                load(admin, config)
                loaded = config
                print("nixant proxy: routes reloaded", file=sys.stderr, flush=True)

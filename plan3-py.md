# plan3.md

Narrowed revision of `plan2.md`, incorporating the review decisions. Where this file is silent, `plan2.md` does not apply; the scope here is deliberately smaller.

## Goal

A small Python CLI that runs NixOS development environments on local Incus.

- Incus system containers first; VMs later (Phase 3).
- NixOS owns guest state. The CLI owns instance lifecycle, host integration, and moving a host-built system into the guest.
- Machine settings live in the NixOS configuration as `nixant.*` options; the CLI reads them as JSON.
- Incus is the only backend. A `Provider` protocol exists, but no second implementation is planned before Phase 5.

Name: `nixant` (CLI, Python package, NixOS options, metadata keys, flake outputs).

## Decisions

| # | Topic | Decision |
|---|---|---|
| 1 | Mount sources | Strings in Nix; relative ones resolved by Python against the project root |
| 2 | Schema | Plain `nixosConfigurations.<target>` + `nixant.nixosModules.{container,vm}` declaring `nixant.*` options |
| 3 | Transport | Build on host; stream missing paths through `incus exec` stdin; activate with `switch-to-configuration` |
| 4 | Naming | `nixant.instanceName` (default: target attribute name); ownership verified via metadata |
| 5 | State | Incus `user.nixant.*` keys only; host GC root under XDG state as a disposable cache |
| 6 | Backend | `Provider` protocol, Incus as the only implementation |
| 7 | Guest user | `nixant.user` with host UID; CLI verifies `uid == os.getuid()` |
| 7 | `exec` syntax | `nixant exec [-n TARGET] CMD...` |
| 7 | Readiness | Retry `incus exec -- true`, then `systemctl is-system-running --wait` (running or degraded); ~60s container, ~180s VM |
| 8 | Ports | Proxy devices listen on `127.0.0.1` unless an address is given |
| 8 | Sizes | Nix normalizes `"4GiB"` etc. to integer bytes |
| 8 | Ordering | Evaluate and build before touching Incus |
| 8 | Models | Frozen `dataclasses`; no Pydantic |
| 8 | Phase 1 | Includes NixOS activation |

---

## Architecture

```text
flake.nix
  nixosConfigurations.<target>  (imports nixant.nixosModules.container)
        |
        |  nix eval --json …config.nixant.runtime
        |  nix build …config.system.build.toplevel
        v
   Python CLI ── planner (small diff) ── IncusProvider ── incus CLI
        |
        +── activate: missing paths ─▶ nix-store --export | incus exec … nix-store --import
                      then nix-env --set + switch-to-configuration
```

Host requirements: Linux x86_64, multi-user Nix with flakes, Incus with the `local` remote and a default profile providing a root disk and NIC. Supported access mode: `incus-admin` group (restricted `incus`-group projects are untested and out of scope).

---

## Nix side

### Tool flake outputs

```text
nixosModules.container   # imports lxc-container.nix + base networking + nixant options
nixosModules.vm          # imports the Incus VM module (agent, boot) + nixant options   (Phase 3)
nixosModules.options     # option declarations only (imported by both)
```

`imports` cannot depend on `config`, so the instance kind is chosen by which module is imported and reported as `config.boot.isContainer`. Kind mismatch between config and instance is therefore impossible to express in Nix; the CLI only has to compare it with an existing instance.

The container module must preserve what the stock image's `/etc/nixos/configuration.nix` provides, because the first activation replaces it entirely:

- `${modulesPath}/virtualisation/lxc-container.nix`
- `systemd.network` DHCP on `eth0`, `networking.useDHCP = false`, `useHostResolvConf = false`
- `networking.hostName` from `nixant.instanceName` when set

The VM module must keep `incus-agent` enabled; without it the CLI loses `incus exec` after the first switch.

### Options

```nix
nixant = {
  enable = true;                       # set by the tool modules; marks a nixant target
  instanceName = null;                 # null → CLI uses the target attribute name
  user = {
    name = "dev";
    uid = 1000;                        # must equal the host UID; CLI checks
    # standard NixOS/home-manager config attaches to users.users.dev / home-manager.users.dev
  };
  cpus = null;                         # Phase 2
  memory = null;                       # "4GiB" or int bytes; Phase 2
  disk = null;                         # Phase 2
  mounts.workspace = { source = "."; target = "/workspace"; readOnly = false; };
  ports = [ ];                         # [{ host = 8080; guest = 80; address = "127.0.0.1"; }]; Phase 2
  runtime = <read-only>;               # normalized attrset consumed by the CLI
};
```

`nixant.user` creates `users.users.<name>` (normal user, given UID, group with same GID, home `/home/<name>`). It is a convenience; users may extend it with any NixOS or home-manager config.

`nixant.runtime` (read-only, `nix eval --json`) contains:

```json
{
  "schemaVersion": 1,
  "kind": "container",
  "instanceName": null,
  "user": { "name": "dev", "uid": 1000, "gid": 1000, "home": "/home/dev", "shell": "/run/current-system/sw/bin/bash" },
  "cpus": null, "memoryBytes": null, "diskBytes": null,
  "mounts": { "workspace": { "source": ".", "target": "/workspace", "readOnly": false } },
  "ports": [],
  "workdir": "/workspace"
}
```

Validation (assertions in Nix): mount targets absolute and unique; port numbers in range and host ports unique; size strings parse; `instanceName` matches Incus name rules (≤63 chars, `[a-z0-9-]`, no leading digit or dash, no trailing dash).

Mount `source` must be a string. Nix path values (`./.`) evaluate to a `/nix/store/…-source` copy, never the checkout, so the option type rejects paths with a message saying so.

### Example project flake

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixant.url = "github:…/nixant";
    home-manager.url = "github:nix-community/home-manager";
  };

  outputs = { nixpkgs, nixant, home-manager, ... }: {
    nixosConfigurations.dev = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        nixant.nixosModules.container
        home-manager.nixosModules.home-manager
        ./nix/dev.nix                                   # project role module
        {
          nixant.user.name = "dev";
          home-manager.users.dev = import ./nix/home.nix;
          services.postgresql.enable = true;
          system.stateVersion = "26.05";
        }
      ];
    };
  };
}
```

Reuse guidance (document, don't enforce): import role modules, not whole host configurations. Host configs carry `hardware-configuration.nix`, `boot.loader`, `fileSystems`, desktop settings that conflict with the container base. Home-manager configs are reusable when the dotfiles flake exports modules (`homeModules.*`) rather than only `homeConfigurations`.

Ship this example in the repository; the integration test uses it.

---

## CLI

```text
nixant up       [TARGET]
nixant rebuild  [TARGET]
nixant shell    [TARGET]
nixant exec     [-n TARGET] CMD...
nixant down     [TARGET]
nixant restart  [TARGET]
nixant destroy  [TARGET]
nixant status   [TARGET]
nixant config   [TARGET]
nixant adopt    [TARGET]
```

- `TARGET` is the `nixosConfigurations` attribute; default `dev`.
- Global: `-v/--verbose` (print every command before running it).
- `exec` takes the target as an option because Click/Typer discard `--`, so a positional `[TARGET] -- CMD` cannot be parsed. `CMD` is everything after options; `--` is accepted and ignored.
- Framework: Typer.

### Project discovery

Walk up from the current directory to the nearest `flake.nix`; that directory is the project root (`Path.resolve()`d). Fail with `no flake.nix found in this directory or its parents`.

Project ID: first 12 hex chars of SHA-256 of the resolved root path.

### Exit codes

`0` success · `1` expected tool error (concise message, stderr of the failing subprocess included where useful) · `2` usage error · `shell`/`exec`: the guest command's status (process replaced via `os.execvp`).

---

## Command behavior

### `up`

```text
1. discover project, eval nixant.runtime            (fail: target missing / module not imported)
2. check user.uid == os.getuid()                     (fail with the option to set)
3. nix build toplevel --out-link $XDG_STATE_HOME/nixant/gcroots/<project-id>-<target>
4. resolve instance name; inspect instance
     exists + not ours        → fail (see Ownership)
     exists + kind differs    → fail: "destroy and up to change kind"
     missing                  → create (stopped) with metadata, nesting, devices
5. reconcile devices/limits (Phase 2; Phase 1 only ensures the workspace mount)
6. start if stopped; readiness
7. activate (skip if /run/current-system already equals the built path)
8. set user.nixant.system=<path>; print name, target, IPv4, `nixant shell` hint
```

Steps 1–3 run before any Incus mutation, so a broken config never leaves a half-created instance. If step 7 fails the instance stays running; the message says to fix the config and run `nixant rebuild`.

Instance states: `Running` → continue; `Stopped` → start; `Frozen` → `incus start` (resumes); `Error` or anything else → fail and print the state.

### `rebuild`

Steps 1–3 and 7–8 of `up`. Requires an owned, running instance; otherwise `instance <name> is not running; run nixant up`. Warns (does not apply) when outer settings differ from the instance.

### `shell`

Requires owned, running instance (no auto-start). Replaces the process with:

```text
incus exec NAME --user UID --group GID --cwd WORKDIR --env HOME=HOME -- SHELL -l
```

`incus shell` is not used: it runs `su -l` and lands in the home directory.

### `exec`

Same preconditions. Runs through a login shell so `/etc/profile` (wrappers, user PATH) applies, without re-quoting:

```text
incus exec NAME --user UID --group GID --cwd WORKDIR --env HOME=HOME -- \
  SHELL -lc 'exec "$@"' nixant CMD...
```

`incus exec` auto-detects TTY, so piping works.

### `down`, `restart`, `destroy`

Ownership required. `down` is idempotent when stopped. `destroy` = `incus delete --force`; succeeds with a note if the instance does not exist; no prompt. Removes the GC-root symlink.

### `status`

Without a target: all instances where `user.nixant.project=<project-id>` (catches instances whose config was removed). With a target: that instance. Output per instance:

```text
supplier-import-dev  RUNNING  container  10.102.97.182
target: dev   system: …-nixos-system-supplier-import-dev-26.05 (current)
root: /home/user/src/supplier-import
```

`(current)` / `(outdated)` compares `user.nixant.system` with the GC-root symlink if present; no evaluation or build. Missing instance: `dev: not created`, exit 0.

### `config`

Prints `nixant.runtime` JSON plus the resolved instance name and absolute mount sources.

### `adopt`

For a moved checkout: if the instance exists, is `managed`, and its `user.nixant.root` no longer exists, rewrite `project`/`root` and mount sources to the current root. Refuses if the old root still exists.

---

## Ownership metadata

Set at creation via `incus create … -c key=value`, so an instance is never visible without them:

```text
user.nixant.managed=true
user.nixant.project=<project-id>
user.nixant.root=<resolved project root>
user.nixant.target=<target>
user.nixant.schema=1
user.nixant.system=<store path>        # updated after each activation
```

Every command that touches an existing instance requires `managed=true` and `project` equal to the current project ID. Errors:

```text
error: instance foo-dev exists but is not managed by nixant
error: instance foo-dev belongs to /other/checkout; set nixant.instanceName or run nixant adopt
```

Tool-managed devices are prefixed `nixant-` (e.g. `nixant-mount-workspace`, `nixant-port-8080`). Reconciliation only adds, changes, or removes prefixed devices.

No local state file. Snapshots (Phase 4) capture the keys with the instance.

---

## Incus provider

```python
class Provider(Protocol):
    def inspect(self, name: str) -> MachineState | None: ...
    def create(self, spec: MachineSpec, metadata: dict[str, str]) -> None: ...
    def apply_changes(self, name: str, changes: list[Change]) -> None: ...
    def start(self, name: str) -> None: ...
    def stop(self, name: str) -> None: ...
    def destroy(self, name: str) -> None: ...
    def run(self, name: str, argv: list[str], *, user: UserSpec | None = None,
            cwd: str | None = None, stdin: IO[bytes] | None = None,
            capture: bool = False) -> CompletedProcess: ...
    def exec_argv(self, name: str, argv: list[str], *, user: UserSpec, cwd: str) -> list[str]: ...
```

`exec_argv` returns the argv for `os.execvp` (shell/exec); `run` is used for readiness and activation. Planning lives in `planner.py`, not in the provider.

Command construction is confined to `providers/incus.py`:

- Always address `local:` explicitly; use the current Incus project.
- Image: `images:nixos/unstable` (only bootstraps the guest; the flake pins the real system). `images:nixos/26.05` also exists.
- Create: `incus create local:IMAGE NAME -c security.nesting=true -c user.nixant.*…`; add devices; then `incus start`.
- VM (Phase 3): `--vm -c security.secureboot=false` (image declares `requirements.secureboot=false`).
- Mounts (containers): `disk source=<abs> path=<target> shift=true [readonly=true]`. If adding with `shift=true` fails, stop with an error. Without shift, files appear as 65534 and are not writable. Verify the mount appeared and retry (see Relation to Incant).
- Ports: `proxy listen=tcp:<address>:<host> connect=tcp:127.0.0.1:<guest>`.
- Inspect via `incus query /1.0/instances/NAME?recursion=1` (status, type, config, devices, IPv4).

## Activation

```text
paths   = nix-store -qR <toplevel>                                  (host)
missing = run(guest, ["nix-store","--check-validity","--print-invalid", *paths])
nix-store --export <missing> | run(guest, ["nix-store","--import"], stdin=pipe)
run(guest, ["nix-env","-p","/nix/var/nix/profiles/system","--set",toplevel])
run(guest, [toplevel+"/bin/switch-to-configuration","switch"])
```

- Guest root is a trusted Nix user, so unsigned imports work. Tested with an 8-path closure on Nix 2.34.
- No flakes or `NIX_CONFIG` in the guest; the guest never evaluates.
- Output of `switch-to-configuration` stays visible; export/import shows a path count and byte total in verbose mode.
- `switch-to-configuration` exit status 4 (some units failed) is reported as a warning, not a failure.

## Readiness

1. Retry `incus exec NAME -- true` (handles `VM agent isn't currently running`).
2. `systemctl is-system-running --wait`; accept `running` or `degraded`, print failed units on `degraded` in verbose mode.
3. Timeout: 60s container, 180s VM; error names the instance and the last observed state.

Run after every start and before activation.

## Reconciliation (Phase 2)

| Field | Container | VM |
|---|---|---|
| cpus, memory | live | live (memory may need restart; verify) |
| disk grow | live | live |
| disk shrink | unsupported → error | unsupported → error |
| mounts add/change/remove | live | restart (verify hotplug) |
| ports | live | live (verify NAT-mode requirement) |
| kind | recreate → refuse, tell user to destroy | same |
| user, NixOS config | activation | activation |

`up` applies live changes, prints restart-required changes and applies them only if the instance was stopped, and refuses recreate-required changes.

---

## Package layout

```text
flake.nix                  # dev shell, package, nixosModules.*
nix/modules/{options,container,vm}.nix
examples/basic/flake.nix
src/nixant/
  __main__.py
  cli.py                   # Typer app, error → exit code mapping
  project.py               # discovery, project ID, instance name, mount path resolution
  models.py                # frozen dataclasses: MachineSpec, MountSpec, PortSpec, UserSpec, MachineState
  errors.py
  run.py                   # single subprocess runner (verbose echo, capture, stdin)
  planner.py               # desired vs actual → list[Change] with classification
  nix/{eval,build,activate}.py
  providers/{base,incus}.py
tests/{unit,integration}/
```

No `commands/` package; command bodies stay in `cli.py` until it exceeds ~400 lines.

Target size for Phases 1–2: ≤2k lines of Python excluding tests.

## Relation to Incant

No Incant code is reused; everything is written fresh. Behavior carried over as knowledge only:

- Disk devices sometimes fail to appear in the guest (incus issue #1881): verify via `/proc/mounts`, remove and re-add the device on failure, bounded retries.
- VM exec before the agent is up fails with `Error: VM agent isn't currently running`; treat it as "not ready yet".
- Inspect instances with `incus query /1.0/instances/NAME?recursion=1` rather than parsing `incus list`.

---

## Phases

### Phase 1: vertical slice (containers)

Nix options module and container module, example flake, discovery, eval, host build, ownership metadata, create, workspace mount, readiness, activation, `nixant.user`, `up`, `rebuild`, `shell`, `exec`, `down`, `destroy`, `status`, `config`.

### Phase 2: machine settings

cpus/memory/disk limits, ports, multiple and read-only mounts, planner with classification, `restart`, `adopt`.

### Phase 3: VMs

`nixosModules.vm`, secure boot setting, VM readiness timeouts, VM mount/port behavior.

### Phase 4: isolation

Snapshots/restore, ephemeral instances, agent-oriented restricted profiles.

### Phase 5: second backend

Only if needed; use it to reshape `Provider`.

---

## Testing

- Unit tests, with the runner mocked: discovery, project ID, instance-name rules, mount resolution, runtime-JSON → dataclass mapping, ownership checks, Incus argv construction, planner classification, `exec` argv parsing.
- Nix tests: `nix flake check` on the tool flake, plus `nix eval --json` of the example's `nixant.runtime` compared against a golden file. Also check that path-valued mount sources and bad sizes are rejected.
- Integration (gated on `NIXANT_INTEGRATION=1`; runnable in the Lima VM): example flake → `up`, `exec -- hostname`, `exec -- touch /workspace/x` (host sees own UID), edit config, `rebuild` (instance creation time unchanged), `down`, `up`, `status`, `destroy`.

## Verified during review

- `images:nixos/unstable` and `nixos/26.05` exist (container + VM); VM image requires `security.secureboot=false`.
- Stock image config: `lxc-container.nix` + systemd-networkd on `eth0`; `nix-command` disabled; `nixos-rebuild` is `nixos-rebuild-ng`; Nix 2.34.
- `incus exec` PATH includes `/run/current-system/sw/bin`, but not `/run/wrappers/bin`.
- `incus exec -- true` succeeds ~2s after start while systemd is still `initializing`.
- With `shift=true`, files written by guest root are host-root-owned; without shift, the workspace appears as 65534 and is read-only. Shift works on the Lima virtiofs mount.
- Guest-root flake eval of a host-owned git repo fails (libgit2 ownership check). Host-side builds avoid this.
- Flake `./.` / `toString ./.` evaluate to a store path.
- Unsigned `nix-store --export | incus exec … nix-store --import` works as guest root.
- Click treats `exec -- hostname` as a missing `COMMAND` and `exec -- ls -la` as target `ls`.

## To verify early

- Throughput of the stdin pipe for a multi-GB first push (Phase 1 spike; fall back to a `file://` cache on a shared mount if too slow).
- Eval time of `config.nixant.runtime` (it runs the NixOS module system on every command that evaluates).
- Whether `security.nesting=true` is strictly required for NixOS containers.
- Hostname handling: `networking.hostName` vs Incus-set hostname.
- VM: module name/path for the Incus VM profile, mount hotplug, proxy NAT-mode requirement.

## Non-goals

Arbitrary guest OSes, provisioners, plugin loading, remote Incus, custom networks/storage pools, image building, generic infrastructure planning, provider feature parity, Incus config passthrough, secrets management, macOS.

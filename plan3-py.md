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
| 4 | Naming | `nixant.instanceName`, default `<project dir>-<target>` (sanitized); ownership verified via metadata |
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
| 9 | Guest sudo | `nixant.user` gets passwordless sudo |
| 10 | Project start | `nixant init [TEMPLATE]` wrapping Nix flake templates exported by the nixant flake |
| 11 | Untracked files | Pre-flight warning for untracked `*.nix` / `flake.lock` before every evaluation |
| 12 | Personalization | Deferred (per-person dotfiles, host UID injection); see Deferred |
| 13 | SSH agent, git identity | Out of scope |

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
templates.default        # minimal container project (Phase 1)
templates.node           # + nodejs                  (Phase 2)
templates.python         # + python3, uv             (Phase 2)
```

`imports` cannot depend on `config`, so the instance kind is chosen by which module is imported and reported as `config.boot.isContainer`. Kind mismatch between config and instance is therefore impossible to express in Nix; the CLI only has to compare it with an existing instance.

The container module must preserve what the stock image's `/etc/nixos/configuration.nix` provides, because the first activation replaces it entirely:

- `${modulesPath}/virtualisation/lxc-container.nix`
- `systemd.network` DHCP on `eth0`, `networking.useDHCP = false`, `useHostResolvConf = false`
- `networking.hostName` from `nixant.instanceName` when set; otherwise left to Incus (the CLI-derived default name is not visible to Nix)

The VM module must keep `incus-agent` enabled; without it the CLI loses `incus exec` after the first switch.

### Options

```nix
nixant = {
  enable = true;                       # set by the tool modules; marks a nixant target
  instanceName = null;                 # null → CLI uses <project dir>-<target>
  user = {
    name = "dev";
    uid = 1000;                        # must equal the host UID; CLI checks
    sudo = true;                       # passwordless sudo
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

`nixant.user` creates `users.users.<name>` (normal user, given UID, group with same GID, home `/home/<name>`, member of `wheel`). With `sudo = true` (default) it adds a `security.sudo.extraRules` NOPASSWD rule for that user only. It is a convenience; users may extend it with any NixOS or home-manager config.

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
          home-manager.users.dev = import ./nix/home.nix;   # team-shared; personal dotfiles are deferred
          services.postgresql.enable = true;
          system.stateVersion = "26.05";
        }
      ];
    };
  };
}
```

Reuse guidance (document, don't enforce): import role modules, not whole host configurations. Host configs carry `hardware-configuration.nix`, `boot.loader`, `fileSystems`, desktop settings that conflict with the container base. Home-manager configs are reusable when the dotfiles flake exports modules (`homeModules.*`) rather than only `homeConfigurations`.

Ship this example in the repository; the integration test uses it. `templates.default` is a trimmed version of it (no home-manager, no postgresql).

---

## CLI

```text
nixant init     [TEMPLATE] [--list]
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

Instance name: `nixant.instanceName` if set, else `<root dir name>-<target>`, sanitized: lowercase, runs of characters outside `[a-z0-9]` become `-`, leading digits/dashes stripped (prefix `n` if nothing remains), truncated to 63 chars without a trailing dash. Two projects with the same directory name clash; the ownership check reports it and suggests setting `nixant.instanceName`.

### Untracked-file pre-flight

Before every evaluation (`up`, `rebuild`, `config`): if the root is inside a git work tree, run `git ls-files --others --exclude-standard -- '*.nix' flake.lock` from the root. If anything is listed, warn:

```text
warning: Nix ignores files not tracked by git:
  nix/dev.nix
run: git add nix/dev.nix
```

Warning only; evaluation proceeds (it may still succeed without the file). If the root is not in a git work tree, warn once per command that Nix will copy the entire directory into the store on every evaluation.

### Exit codes

`0` success · `1` expected tool error (concise message, stderr of the failing subprocess included where useful) · `2` usage error · `shell`/`exec`: the guest command's status (process replaced via `os.execvp`).

---

## Command behavior

### `init`

```console
$ nixant init --list
default   minimal container, user with sudo
node      + nodejs
python    + python3, uv

$ nixant init            # = nixant init default
wrote flake.nix nix/dev.nix
added to git: flake.nix nix/dev.nix
next: nixant up
```

- Runs in the current directory; no project discovery.
- Template source: the nixant flake the CLI was built from. The package bakes in its own source store path, so `init` works offline and templates always match the CLI version. `--list` reads `templates` descriptions via `nix eval --json`.
- Runs `nix flake init -t <self>#<TEMPLATE>`, then makes sure the created files are tracked if the directory is a git work tree. Outside git, prints the whole-directory-copy warning and suggests `git init`.
- The template's `nixant.url` is unversioned. `init` runs `nix flake lock` so the lock file pins it immediately, and tracks `flake.lock` too. A later CLI/library mismatch is caught by `schemaVersion`; the error suggests `nix flake update nixant` or upgrading the CLI.
- If `flake.nix` already exists, nothing is written. It prints the inputs and `nixosConfigurations.dev` block to add by hand; the snippets ship as package data next to the templates.
- Unknown template: error listing the available names.

### `up`

```text
1. discover project, untracked-file pre-flight, eval nixant.runtime
                                                     (fail: target missing / module not imported)
2. check user.uid == os.getuid()                     (fail with the option to set)
3. nix build toplevel --out-link $XDG_STATE_HOME/nixant/gcroots/<project-id>-<target>
4. resolve the instance (see Instance lookup, evaluating form)
     exists + kind differs    → fail: "destroy and up to change kind"
     missing                  → create (stopped) with metadata, nesting, devices
5. reconcile devices/limits (Phase 2; Phase 1 only ensures the workspace mount)
6. start if stopped; readiness
7. activate, unless the skip condition holds (see Activation)
8. print name, target, IPv4, `nixant shell` hint
```

Steps 1–3 run before any Incus mutation, so a broken config never leaves a half-created instance. If step 7 fails the instance stays running and the recorded activation state is not `ok`, so the next `up` retries; the message says to fix the config and run `nixant rebuild`.

Instance states: `Running` → continue; `Stopped` → start; `Frozen` → `incus start` (resumes); `Error` or anything else → fail and print the state.

### `rebuild`

Steps 1–3 and 7–8 of `up`, but always activates (never takes the skip shortcut), so it is the explicit retry. Requires an owned, running instance; otherwise `instance <name> is not running; run nixant up`. Warns (does not apply) when outer settings differ from the instance.

### `shell`

Does not evaluate; finds the instance by metadata (see Instance lookup). Requires a running instance (no auto-start) and a recorded successful activation (`user.nixant.user` set); otherwise `no successful activation yet; run nixant up`. Replaces the process with:

```text
incus exec local:NAME --cwd WORKDIR -- /run/current-system/sw/bin/runuser -u USER -- \
  /run/current-system/sw/bin/bash -c 'exec -l "$(getent passwd "$(id -un)" | cut -d: -f7)"'
```

- `USER` and `WORKDIR` come from `user.nixant.user` / `user.nixant.workdir`.
- `runuser -u` initializes supplementary groups and sets `HOME`, and keeps the working directory. `incus exec --user/--group` does neither (tested: `groups=1000(dev)` without `wheel`, `HOME` empty).
- `exec -l` starts the user's configured login shell with a `-` argv0, so bash, zsh, and fish all start as login shells.
- `incus shell` is not used: it runs `su -l` and lands in the home directory.

### `exec`

Same lookup and preconditions. Always uses bash as the execution shell, independent of the user's interactive shell, so the wrapper is valid whatever `users.users.<name>.shell` is:

```text
incus exec local:NAME --cwd WORKDIR -- /run/current-system/sw/bin/runuser -u USER -- \
  /run/current-system/sw/bin/bash -lc 'exec "$@"' nixant CMD...
```

The bash login sources `/etc/profile` (NixOS environment, `/run/wrappers/bin`). Settings defined only in the user's non-bash shell config (e.g. fish-only variables) do not apply to `exec`; documented.

`incus exec` auto-detects TTY, so piping works.

### `down`, `restart`, `destroy`

Do not evaluate; find the instance by metadata, so a broken or removed configuration never blocks cleanup. `down` is idempotent when stopped. `destroy` = `incus delete --force local:NAME`; succeeds with a note if no instance matches; no prompt. Removes the GC-root symlink.

### `status`

Does not evaluate or build. Without a target: all instances where `user.nixant.project=<project-id>` (catches instances whose config was removed). With a target: the metadata lookup for that target. Output per instance:

```text
supplier-import-dev  RUNNING  container  10.102.97.182
target: dev   system: …-nixos-system-supplier-import-dev-26.05 (ok, current)
root: /home/user/src/supplier-import
```

The first word in parentheses is `user.nixant.activation`. `current` / `outdated` compares `user.nixant.system` with the GC-root symlink if present. Missing instance: `dev: not created`, exit 0.

### `config`

Prints `nixant.runtime` JSON plus the resolved instance name and absolute mount sources.

### `adopt`

`nixant adopt [TARGET] [--instance NAME]`. For a moved checkout. Candidates: managed instances with `user.nixant.target=<TARGET>` whose `user.nixant.root` no longer exists. Exactly one candidate, or the one named by `--instance`: rewrite `project`/`root` and mount sources to the current root. Several candidates without `--instance`: error listing them. Refuses if the old root still exists.

---

## Ownership metadata

Set at creation via `incus create … -c key=value`, so an instance is never visible without them:

```text
user.nixant.managed=true
user.nixant.project=<project-id>
user.nixant.root=<resolved project root>
user.nixant.target=<target>
user.nixant.schema=1
```

Written by activation (see Activation):

```text
user.nixant.activation=pending|ok|degraded|failed
user.nixant.system=<store path>        # last system activated with result ok or degraded
user.nixant.user=<guest user name>     # used by shell/exec without evaluation
user.nixant.workdir=<path>
```

An instance belongs to a target only if `managed=true`, `project` equals the current project ID, **and** `target` equals the target. Checking the project alone is not enough: two targets of one project can be configured with the same `instanceName`. Errors:

```text
error: instance foo-dev exists but is not managed by nixant
error: instance foo-dev belongs to /other/checkout; set nixant.instanceName or run nixant adopt
error: instance foo-dev belongs to target test of this project; give dev a different nixant.instanceName
```

Tool-managed devices are prefixed `nixant-` (e.g. `nixant-mount-workspace`, `nixant-port-8080`). Reconciliation only adds, changes, or removes prefixed devices. Single exception: the instance-local `root` disk device, and only its `size` key (see Reconciliation). Profiles are never modified.

No local state file. Snapshots (Phase 4) capture the keys with the instance.

### Instance lookup

**Metadata form** (`shell`, `exec`, `down`, `restart`, `destroy`, `status`, no evaluation): `incus list local: user.nixant.project=<id> user.nixant.target=<target> --format json`.

- 0 matches → `dev: not created` (`destroy`: note, exit 0; others: `environment does not exist; run nixant up`).
- 1 match → that instance.
- More than 1 (e.g. `incus copy`, or an old instance left after `instanceName` changed) → error listing the names, suggesting `incus delete local:NAME` for the stale one.

**Evaluating form** (`up`, `rebuild`): run the metadata lookup, then compare with the configured name `N`.

| Metadata matches | Instance named `N` | Result |
|---|---|---|
| none | absent | create `N` |
| none | exists | fail: not managed / other checkout / other target (errors above) |
| exactly `N` | — | use it |
| one, named `X ≠ N` | — | fail: `target dev's instance is X but the config now names N; run nixant destroy dev or restore nixant.instanceName` |
| several | — | fail as in the metadata form |

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
    def run(self, name: str, argv: list[str], *, user: str | None = None,
            cwd: str | None = None, stdin: IO[bytes] | None = None,
            capture: bool = False) -> CompletedProcess: ...
    def exec_argv(self, name: str, argv: list[str], *, user: str, cwd: str) -> list[str]: ...
    def find(self, metadata: dict[str, str]) -> list[MachineState]: ...
```

`exec_argv` returns the argv for `os.execvp` (shell/exec); `run` is used for readiness and activation. Planning lives in `planner.py`, not in the provider.

Command construction is confined to `providers/incus.py`:

- Always qualify instances as `local:NAME`; use the current Incus project.
- Image: `images:nixos/unstable` (only bootstraps the guest; the flake pins the real system). `images:nixos/26.05` also exists.
- Create: `incus create images:nixos/unstable local:NAME -c security.nesting=true -c user.nixant.*…`; add devices; then `incus start local:NAME`. The image is named with its `images:` remote and the destination with `local:`. A bare `local:IMAGE` would search the local image store instead.
- VM (Phase 3): `--vm -c security.secureboot=false` (image declares `requirements.secureboot=false`).
- Mounts (containers): `disk source=<abs> path=<target> shift=true [readonly=true]`. If adding with `shift=true` fails, stop with an error. Without shift, files appear as 65534 and are not writable. Verify the mount appeared and retry (see Relation to Incant).
- Ports: `proxy listen=tcp:<address>:<host> connect=tcp:127.0.0.1:<guest>`.
- Inspect via `incus query /1.0/instances/NAME?recursion=1` (status, type, config, devices, IPv4).

## Activation

```text
paths   = nix-store -qR <toplevel>                                  (host)
missing = run(guest, ["nix-store","--check-validity","--print-invalid", *paths])
nix-store --export <missing> | run(guest, ["nix-store","--import"], stdin=pipe)
set user.nixant.activation=pending
run(guest, ["nix-env","-p","/nix/var/nix/profiles/system","--set",toplevel])
rc = run(guest, [toplevel+"/bin/switch-to-configuration","switch"])
record result (below)
```

Recording, based on the `switch-to-configuration` exit status:

| Exit | `activation` | Also set | CLI result |
|---|---|---|---|
| 0 | `ok` | `system`, `user`, `workdir` | success |
| 4 (some units failed) | `degraded` | `system`, `user`, `workdir` | warning, failed units listed, exit 0 |
| other / interrupted / import failed | `failed` (or stays `pending`) | nothing else | error, exit 1 |

Skip condition (`up` only): skip activation only if `activation=ok` **and** `system` equals the built path **and** `/run/current-system` equals the built path. `/run/current-system` alone is not evidence of success: the activation script updates it before `switch-to-configuration` finishes restarting units, so a failed switch can leave it pointing at the new system. `degraded`, `failed`, and `pending` never skip, so `up` retries them. `rebuild` never skips.

- Guest root is a trusted Nix user, so unsigned imports work. Tested with an 8-path closure on Nix 2.34.
- No flakes or `NIX_CONFIG` in the guest; the guest never evaluates.
- Output of `switch-to-configuration` stays visible; export/import shows a path count and byte total in verbose mode.

## Readiness

1. Retry `incus exec local:NAME -- true` (handles `VM agent isn't currently running`).
2. `systemctl is-system-running --wait`; accept `running` or `degraded`, print failed units on `degraded` in verbose mode.
3. Timeout: 60s container, 180s VM; error names the instance and the last observed state.

Run after every start and before activation.

## Reconciliation (Phase 2)

| Field | Container | VM |
|---|---|---|
| cpus, memory | live | live (memory may need restart; verify) |
| disk grow | live (see Root disk) | live (see Root disk) |
| disk shrink | unsupported → error | unsupported → error |
| mounts add/change/remove | live | restart (verify hotplug) |
| ports | live | live (verify NAT-mode requirement) |
| kind | recreate → refuse, tell user to destroy | same |
| user, NixOS config | activation | activation |

`up` applies live changes, prints restart-required changes and applies them only if the instance was stopped, and refuses recreate-required changes.

### Root disk

The root disk comes from the default profile, so it is not a `nixant-` device.

- At creation with `nixant.disk` set: `incus create … -d root,size=<bytes>`. This makes an instance-local override of the inherited device.
- Later changes: if `root` is still inherited, `incus config device override local:NAME root size=<bytes>`. Otherwise `incus config device set local:NAME root size=<bytes>`.
- nixant only touches the `size` key of the instance-local `root` device. It never edits the profile, which is shared with unrelated instances.
- `nixant.disk = null` leaves the root device alone. An existing override is not removed.
- Pools without quota support: before applying, read the root pool's driver (`incus storage show`). If the driver cannot enforce a size limit (`dir`), fail with `nixant.disk is not supported on storage pool <pool> (driver dir); unset it or use a btrfs/zfs/lvm pool`. Never silently ignore the setting. Exact per-driver support to be verified in Phase 2.

---

## Package layout

```text
flake.nix                  # dev shell, package, nixosModules.*
nix/modules/{options,container,vm}.nix
nix/templates/{default,node,python}/   # flake templates
nix/snippets/{default,node,python}.nix # printed by `init` when flake.nix exists
examples/basic/flake.nix
src/nixant/
  __main__.py
  cli.py                   # Typer app, error → exit code mapping
  project.py               # discovery, project ID, instance name, mount path resolution, untracked pre-flight
  init.py                  # template listing, nix flake init, snippets
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

Nix options module and container module, example flake, `templates.default`, discovery, untracked pre-flight, eval, host build, ownership metadata, create, workspace mount, readiness, activation, `nixant.user` (with sudo), `init`, `up`, `rebuild`, `shell`, `exec`, `down`, `destroy`, `status`, `config`.

### Phase 2: machine settings

cpus/memory/disk limits, ports, multiple and read-only mounts, planner with classification, `restart`, `adopt`, `node` and `python` templates.

### Phase 3: VMs

`nixosModules.vm`, secure boot setting, VM readiness timeouts, VM mount/port behavior.

### Phase 4: isolation

Snapshots/restore, ephemeral instances, agent-oriented restricted profiles.

### Phase 5: second backend

Only if needed; use it to reshape `Provider`.

---

## Testing

- Unit tests, with the runner mocked: discovery, project ID, instance-name derivation and sanitization, mount resolution, untracked pre-flight (temporary git repo), runtime-JSON → dataclass mapping, ownership checks, Incus argv construction, planner classification, `exec` argv parsing, `init` with and without an existing `flake.nix`.
- Nix tests: `nix flake check` on the tool flake, plus `nix eval --json` of the example's `nixant.runtime` compared against a golden file. Also check that path-valued mount sources and bad sizes are rejected, and that every template evaluates.
- Unit tests, ownership and lookup (mocked `incus list`/`query` output):
  - Two targets with the same `instanceName`: the second `up` fails and the second target's `rebuild`/`destroy` never touch the first target's instance.
  - Every row of the evaluating-lookup table; 0/1/many metadata matches.
  - Ownership check rejects a project-ID match when the target differs.
- Unit tests, activation recording: exit 0 → `ok`; exit 4 → `degraded`; other exit, import failure, or interruption → never `ok`. The skip condition is false unless all three parts hold, and `rebuild` never skips.
- Integration (gated on `NIXANT_INTEGRATION=1`; runnable in the Lima VM):
  - Happy path: temporary git repo → `init` → `up`, `exec -- hostname`, `exec -- touch /workspace/x` (host sees own UID), `exec -- sudo true`, `exec -- id -Gn` (includes `wheel`), edit config, `rebuild` (instance creation time unchanged), `down`, `up`, `status`, `destroy`.
  - Failure recovery:
    - Add a systemd service that fails → `up` reports `degraded`; a second `up` re-activates instead of skipping; fixing the service → `ok`.
    - Interrupt `up` during import → `up` again completes.
    - Introduce a Nix syntax error → `status`, `down`, `destroy` still work.
  - Cross-target: two targets with distinct names coexist; giving them the same `instanceName` makes the second `up` fail without modifying the first instance.
  - Shell independence: set the user's shell to fish → `exec -- true` still works, and `shell` starts fish.

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
- `incus exec --user 1000 --group 1000` gives `groups=1000(dev)` (no `wheel`) and an empty `HOME`. `runuser -u dev` gives `groups=1000(dev),1(wheel)`, `HOME=/home/dev`, keeps `--cwd`, and works with a TTY.
- `incus create images:nixos/unstable local:NAME` works.

## To verify early

- Throughput of the stdin pipe for a multi-GB first push (Phase 1 spike; fall back to a `file://` cache on a shared mount if too slow).
- Eval time of `config.nixant.runtime` (it runs the NixOS module system on every command that evaluates).
- Whether `security.nesting=true` is strictly required for NixOS containers.
- Hostname handling: `networking.hostName` vs Incus-set hostname.
- Exactly when the activation script updates `/run/current-system` relative to unit restarts. The skip condition does not depend on the answer, but error messages might.
- Root-disk `size` support per storage driver (Phase 2).
- VM: module name/path for the Incus VM profile, mount hotplug, proxy NAT-mode requirement.

## Deferred: personalization

Per-person settings stay out of the shared project flake, so nixant has no mechanism for them yet. Interim behavior: the shared config sets `nixant.user.uid` (default 1000). A developer whose host UID differs gets the UID-mismatch error and has to change the shared value. Personal dotfiles can only be added as team-shared home-manager config.

A git-ignored `local.nix` cannot be the answer: git flakes do not see untracked or ignored files.

Future options:

1. **CLI injects via `extendModules`.** Build `nixosConfigurations.<t>.extendModules { modules = [ { nixant.user.uid = <host uid>; nixant.user.name = <host user>; } ~/.config/nixant/user.nix ]; }` with an impure `--expr`. The UID never appears in the repo. The personal module cannot bring its own flake inputs, and the Nix eval cache is lost.
2. **Personal flake via `--override-input`.** The tool modules read an `nixant-user` input defaulting to an empty flake shipped by nixant; the CLI passes `--override-input nixant-user ~/.config/nixant` when it exists. The personal flake can have its own inputs (dotfiles, home-manager), and evaluation stays pure, but it needs more wiring and care to avoid writing the override into `flake.lock`.
3. **UID-only injection.** Option 1 restricted to `uid`/`name`; dotfiles remain a team decision.

## Non-goals

Arbitrary guest OSes, provisioners, plugin loading, remote Incus, custom networks/storage pools, image building, generic infrastructure planning, provider feature parity, Incus config passthrough, secrets management, macOS, SSH agent forwarding, git identity/credentials inside the guest.

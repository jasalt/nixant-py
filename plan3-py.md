# nixant: plan 3 (Python)

Narrowed revision of `plan2-py.md`, incorporating the review decisions. Where this file is silent, `plan2-py.md` does not apply; the scope here is deliberately smaller.

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
| 4 | Naming | `nixant.instanceName` is required (assertion) and also sets the guest hostname; `init` fills in `<project dir>-dev`; ownership verified via metadata |
| 5 | State | Incus `user.nixant.*` keys only; host GC root under XDG state as a disposable cache |
| 6 | Backend | `Provider` protocol, Incus as the only implementation |
| 7 | Guest user | `nixant.user` with host UID; CLI verifies `uid == os.getuid()` |
| 8 | `exec` syntax | `nixant exec [-n TARGET] CMD...` |
| 9 | Readiness | Retry `incus exec -- true`, then `systemctl is-system-running --wait` (running or degraded); ~60s container, ~180s VM |
| 10 | Ports | Proxy devices listen on `127.0.0.1` unless an address is given |
| 11 | Sizes | Nix normalizes `"4GiB"` etc. to integer bytes |
| 12 | Ordering | Evaluate and build before touching Incus |
| 13 | Models | Frozen `dataclasses`; no Pydantic |
| 14 | Phase 1 | Includes NixOS activation |
| 15 | Guest sudo | `nixant.user` gets passwordless sudo |
| 16 | Project start | `nixant init [TEMPLATE]` wrapping Nix flake templates exported by the nixant flake |
| 17 | Untracked files | Pre-flight warning for untracked `*.nix` / `flake.lock` before every evaluation |
| 18 | Personalization | Deferred (per-person dotfiles, host UID injection); see Deferred |
| 19 | SSH agent, git identity | Out of scope |
| 20 | Concurrency | Per-target lock file for mutating commands |
| 21 | Distribution | Nix only; package bakes in its own source path and revision |
| 22 | Version pin | Template keeps an unversioned `nixant.url`; `init` locks it to the CLI's revision via `--override-input` |
| 23 | Activation bound | No default timeout; `up`/`rebuild --timeout DURATION` for scripts and agents |
| 24 | Reboot-required switch | Exit 100 → automatic `incus restart`, then verify and record |
| 25 | Guest-side Nix | Supported: flakes enabled, user trusted, nesting required (verified) |
| 26 | `/etc/nixos` | Activation installs a throwing stub; stock files removed |
| 27 | Bootstrap image churn | Accepted and documented; nixant never changes Incus server config |
| 28 | Second checkout on one host | Per-checkout git config override of the instance name, set via `nixant name`; `status --orphans` |

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
- `networking.hostName = mkDefault config.nixant.instanceName`, so the hostname, the system store name (`…-nixos-system-<instanceName>-<release>`), and the Incus instance name are identical
- the nixpkgs release assertion (≥ 25.05, see Activation)
- guest Nix for the user (see below): `nix.settings.experimental-features = mkDefault [ "nix-command" "flakes" ]`
- a stale-config guard for `/etc/nixos` (see below)

The VM module must keep `incus-agent` enabled; without it the CLI loses `incus exec` after the first switch.

### Guest-side Nix

Running `nix build` / `nix develop` as the user inside the guest is supported. It uses the guest's own Nix daemon and store, separate from the host's.

- **Flakes and `nix-command`** are enabled by default (`mkDefault`).
- **`nix.settings.trusted-users = mkDefault [ "root" <nixant.user.name> ]`.** This adds no privilege, since the user already has passwordless sudo. It lets a project flake's `nixConfig.extra-substituters` take effect.
- **Substituters:** cache.nixos.org by default (the stock image's setting); projects add their own through normal NixOS or flake config.
- **`security.nesting=true` is required for this.** Sandboxed builds as a non-root user succeed with nesting and fail without it ("this system does not support the kernel namespaces that are required for sandboxing"); the guest boots either way (verified).
- **Workspace ownership:** the workspace is owned by the same UID as the guest user (shifted mount). Flake evaluation of `/workspace` inside the guest therefore avoids the libgit2 ownership error that guest root hits.

### Stale `/etc/nixos`

After the first activation, the stock `/etc/nixos/configuration.nix` and `incus.nix` are dead: nothing imports them. Running plain `nixos-rebuild switch` inside the guest would silently revert to the stock system.

On every activation, an activation script in the container module:
- replaces `/etc/nixos/configuration.nix` with a stub that evaluates to `throw "This system is managed by nixant (instance <instanceName>). Edit the project flake and run `nixant rebuild` on the host."`
- removes `/etc/nixos/incus.nix`
- writes `/etc/nixos/README`

Incus regenerates `incus.nix` only on `create` and `copy` (image template `when: [create, copy]`, verified), so re-running the script on each activation also covers `incus copy`.

### Options

```nix
nixant = {
  enable = true;                       # set by the tool modules; marks a nixant target
  instanceName = "shop-dev";           # required; Incus instance name and guest hostname
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
  workdir = null;                      # null → mounts.workspace.target if that mount exists, else user's home
  ports = [ ];                         # [{ host = 8080; guest = 80; address = "127.0.0.1"; }]; Phase 2
  runtime = <read-only>;               # normalized attrset consumed by the CLI
};
```

`nixant.user` creates `users.users.<name>` (normal user, given UID, group with same GID, home `/home/<name>`, member of `wheel`, Nix trusted user). With `sudo = true` (default) it adds a `security.sudo.extraRules` NOPASSWD rule for that user only. It is a convenience; users may extend it with any NixOS or home-manager config. `name = "root"` and `uid = 0` are rejected by assertion: `shell`/`exec` always enter as this non-root user, and the UID must equal an unprivileged host UID.

`nixant.workdir` is the working directory for `shell` and `exec`. Renaming or removing the `workspace` mount does not break it: without that mount it falls back to the user's home. An explicit value must be an absolute path.

`nixant.runtime` (read-only, `nix eval --json`) contains:

```json
{
  "schemaVersion": 1,
  "kind": "container",
  "instanceName": "shop-dev",
  "user": { "name": "dev", "uid": 1000, "gid": 1000, "home": "/home/dev", "shell": "/run/current-system/sw/bin/bash" },
  "cpus": null, "memoryBytes": null, "diskBytes": null,
  "mounts": { "workspace": { "source": ".", "target": "/workspace", "readOnly": false } },
  "ports": [],
  "workdir": "/workspace"
}
```

Validation (assertions in Nix):
- `instanceName` is set and matches Incus name rules (≤63 chars, `[a-z0-9-]`, no leading digit or dash, no trailing dash). When unset, the error names the line to add.
- Mount targets are absolute and unique.
- Port numbers are in range and host ports unique.
- Size strings parse.

Mount `source` must be a string. Nix path values (`./.`) evaluate to a `/nix/store/…-source` copy, never the checkout, so the option type rejects paths with a message saying so.

### Example project flake

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixant.url = "github:…/nixant";
    nixant.inputs.nixpkgs.follows = "nixpkgs";
    home-manager.url = "github:nix-community/home-manager";
    home-manager.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { nixpkgs, nixant, home-manager, ... }: {
    nixosConfigurations.dev = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        nixant.nixosModules.container
        home-manager.nixosModules.home-manager
        ./nix/dev.nix                                   # project role module
        {
          nixant.instanceName = "shop-dev";
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

Ship this example in the repository; the integration test uses it. `templates.default` is a trimmed version of it (no home-manager, no postgresql). Templates and example always set the `follows` lines, so a project lock holds one nixpkgs.

---

## CLI

```text
nixant init     [TEMPLATE] [--list]
nixant up       [TARGET] [--timeout DURATION]
nixant rebuild  [TARGET] [--timeout DURATION]
nixant shell    [TARGET]
nixant exec     [-n TARGET] CMD...
nixant down     [TARGET] [--force]
nixant restart  [TARGET] [--force]
nixant destroy  [TARGET]
nixant status   [TARGET] [--orphans]
nixant config   [TARGET]
nixant adopt    [TARGET] [--instance NAME]
nixant name     [TARGET] [NAME | --unset]
```

- `TARGET` is the `nixosConfigurations` attribute; default `dev`. It must match `[A-Za-z_][A-Za-z0-9_-]*` (a plain Nix identifier), so the CLI never has to quote it in installables. Other names are rejected with a usage error.
- Global: `-v/--verbose` (print every command before running it).
- `exec` takes the target as an option because Click/Typer discard `--`, so a positional `[TARGET] -- CMD` cannot be parsed. `CMD` is everything after options; `--` is accepted and ignored.
- Framework: Typer.

### Project discovery

Walk up from the current directory to the nearest `flake.nix`; that directory is the project root (`Path.resolve()`d). Fail with `no flake.nix found in this directory or its parents`.

Project ID: first 12 hex chars of SHA-256 of the resolved root path.

Instance name: the per-checkout override if one is set, otherwise `nixant.instanceName` from the evaluated config. The CLI never derives a name by itself. Renaming or moving the project directory therefore does not change the instance (`adopt` only fixes the root path).

Override lookup: `git config --get nixant.<target>.instanceName`, run from the project root and skipped outside a git work tree. It reads the clone's `.git/config` or, with `extensions.worktreeConfig`, the worktree's own config. Overrides go through the same Incus name validation as the Nix option. They are user configuration kept by git, not nixant state, and are written only by `nixant name`.

The name is committed, so it is shared by everyone using the repository. That is fine across machines, because Incus names are per host. On one host, two projects (or two git worktrees or clones of the same repository) with the same `instanceName` clash. The ownership check reports it and suggests `nixant name <target>`, which gives this checkout its own name without touching tracked files.

Limitation: the override is invisible to pure evaluation. The guest hostname and system store name keep the committed `instanceName`, so both guests show the same prompt while Incus calls them by different names. `status` and `shell`'s ready message show the effective name. Making the hostname follow the override is left to personalization (see Deferred).

### Locking

Mutating commands (`up`, `rebuild`, `down`, `restart`, `destroy`, `adopt`, `name`) hold an exclusive `flock` on `$XDG_STATE_HOME/nixant/locks/<project-id>-<target>.lock` for their whole run. If the lock is taken, they fail at once with `another nixant command is running for target dev`. This prevents two `switch-to-configuration` runs in one guest and racing creates. `shell`, `exec`, `status`, `config`, and `init` take no lock.

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
wrote flake.nix nix/dev.nix (instanceName: shop-dev)
locked nixant to 3f2a9c1 (this CLI)
added to git: flake.nix nix/dev.nix flake.lock
next: nixant up
```

- Runs in the current directory; no project discovery.
- **Template source:** the nixant flake the CLI was built from (`NIXANT_SELF`, see Packaging), so templates always match the CLI version. `--list` reads `templates` descriptions via `nix eval --json`. Copying the template is local.
- **Copy:** runs `nix flake init -t path:$NIXANT_SELF#<TEMPLATE>`.
- **Instance name:** sets `nixant.instanceName` to `<dir>-dev`. Templates carry the marker value `"nixant-template-dev"`, which keeps every template evaluable in `nix flake check`; `init` rewrites that string literal.
  - The proposal is sanitized: lowercase; runs outside `[a-z0-9]` become `-`; leading digits and dashes are stripped (prefix `n` if nothing remains); truncated to 63 chars without a trailing dash.
  - If an Incus instance with that name already exists, `init` warns and suggests editing the name before `up`.
- **Lock, pinned to the CLI's revision:** `nix flake lock --override-input nixant github:…/nixant/$NIXANT_REV`.
  - `flake.nix` keeps the unversioned `nixant.url`, while the lock records this revision. Later evaluations and plain `nix flake lock` keep it, and `nix flake update nixant` upgrades normally (tested with local repos).
  - Project and CLI therefore match at creation; `schemaVersion` guards later drift, with an error suggesting `nix flake update nixant` or a matching CLI.
  - This step needs network access (nixpkgs is fetched). If it fails, the written files stay and `init` prints the exact lock command to run later.
  - With an empty `NIXANT_REV` (a dirty dev build), it does a plain `nix flake lock` and warns that the project is pinned to the latest nixant, not to this CLI.
- **Git:** makes sure the created files and `flake.lock` are tracked if the directory is a git work tree. Outside git, prints the whole-directory-copy warning and suggests `git init`.
- **Existing `flake.nix`:** nothing is written. It prints the inputs (with `follows`) and a `nixosConfigurations.dev` block, with the proposed `instanceName` filled in, to add by hand. The snippets ship next to the templates.
- **Unknown template:** error listing the available names.

### `up`

```text
1. take the target lock; discover project, untracked-file pre-flight;
   evaluate once: { runtime = config.nixant.runtime; drvPath = toplevel.drvPath; }
     target missing           → fail, listing available targets
     module not imported      → fail naming nixant.nixosModules.container
2. check user.uid == os.getuid()                     (fail with the option to set)
   check every mount source exists on the host       (fail naming the option and path)
3. nix build '<drvPath>^out' --out-link $XDG_STATE_HOME/nixant/gcroots/<project-id>-<target>
4. resolve the instance (see Instance lookup, evaluating form)
     schema differs           → fail (see Schema version)
     exists + kind differs    → fail: "destroy and up to change kind"
     missing                  → create (stopped) with metadata, nesting, devices
5. reconcile devices/limits (Phase 2; Phase 1 only ensures the workspace mount)
6. start if stopped; readiness
7. activate, unless the skip condition holds (see Activation)
8. print name, target, IPv4, `nixant shell` hint
```

Steps 1–3 run before any Incus mutation, so a broken config never leaves a half-created instance. If step 7 fails the instance stays running and the recorded activation state is not `ok`, so the next `up` retries; the message says to fix the config and run `nixant rebuild`.

Available targets for the target-missing error come from `nix eval --json .#nixosConfigurations --apply builtins.attrNames`. This does not force any configuration, so it stays cheap. It lists all configurations, including ones that don't import a nixant module.

### Evaluation cost

Evaluating a NixOS configuration is the main latency. The external simulation measured about 40s cold (fresh clone: fetching inputs plus the module system) and seconds when warm.

- **One evaluation per command.**
  - `up`/`rebuild` use a single `nix eval --json .#nixosConfigurations.<t> --apply 'c: { runtime = …; drvPath = …; }'`. Building by `.drv` path needs no second evaluation.
  - `config` evaluates only `runtime`, which is lazier than the full system.
- **Never silent.** Before evaluating, print `evaluating <target>…`, and pass Nix's stderr (fetch and progress lines) through to the terminal instead of capturing it. A cold first run then shows what it is waiting for.
- **Eval cache:** Nix's flake eval cache does not apply to `--apply` expressions or dirty git trees. Accepted for Phases 1–2; re-measure early in Phase 1 (see To verify early).

Instance states: `Running` → continue; `Stopped` → start; `Frozen` → `incus start` (resumes; verified); `Error` or anything else → fail and print the state.

`incus create`, `start`, and `delete` run without a nixant timeout, with output streamed. Their duration depends on the storage driver and image download.

### `rebuild`

Steps 1–4 and 7–8 of `up`, without creating, but always activates (never takes the skip shortcut), so it is the explicit retry. Requires an owned, running instance; otherwise `instance <name> is not running; run nixant up`. Warns (does not apply) when outer settings differ from the instance.

### `shell`

Does not evaluate; finds the instance by metadata (see Instance lookup). Requires a running instance (no auto-start) and at least one activation recorded as `ok` or `degraded` (`user.nixant.user` set); otherwise `no completed activation yet; run nixant up`. Replaces the process with:

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

Do not evaluate; find the instance by metadata, so a broken or removed configuration never blocks cleanup. None of them re-activates.

- **`down [--force]`**:
  - `Stopped` → note, exit 0. `incus stop` itself exits 1 on a stopped instance, so check the state first.
  - `Frozen` → `incus start` (resume) first, because a clean stop of a frozen container does not complete (verified).
  - Then `incus stop --timeout 60 local:NAME`. On timeout, fail with `did not shut down within 60s; run nixant down --force`.
  - `--force` → `incus stop --force`.
- **`restart [--force]`**:
  - `Stopped` → `incus start`.
  - `Frozen` → resume first.
  - Running → `incus restart --timeout 60` (`--force` → `incus restart --force`).
  - Always followed by readiness, since the guest has just booted.
- **`destroy`**: `incus delete --force local:NAME` in any state; succeeds with a note if no instance matches; no prompt. Removes the GC-root symlink.

### `status`

Does not evaluate or build. Without a target: all instances where `user.nixant.project=<project-id>` (catches instances whose config was removed). With a target: the metadata lookup for that target. Output per instance:

```text
supplier-import-dev  RUNNING  container  10.102.97.182
target: dev   system: …-nixos-system-supplier-import-dev-26.05 (ok, current)
root: /home/user/src/supplier-import
```

The first word in parentheses is `user.nixant.activation`. `current` / `outdated` compares `user.nixant.system` with the GC-root symlink if present. Missing instance: `dev: not created`, exit 0. An instance named by an override is marked `(override)`.

`--orphans` lists all managed instances on the host (`user.nixant.managed=true`) whose `user.nixant.root` no longer exists, typically left behind by `git worktree remove` or a deleted clone. It shows name, target, and the old root, and suggests `incus delete --force local:NAME` or `nixant adopt`.

### `config`

Prints `nixant.runtime` JSON plus the project root, project ID, effective instance name and its source (`config` or `git override`), and absolute mount sources.

### `name`

`nixant name [TARGET] [NAME | --unset]` sets this checkout's instance-name override for a target. It does not evaluate and takes the target lock.

- **Without `NAME`:** uses `<committed instanceName>-<checkout dir name>`, sanitized, and prints it. No prompt; run again with `NAME` to choose another.
- **Where it writes:**
  - In a linked worktree, or a repository with several worktrees: enables `extensions.worktreeConfig` if needed, then `git config --worktree`.
  - Otherwise, the clone's own `git config`.
  - It prints which one it used.
- **Refuses** to rename an existing instance: if this checkout already owns one for the target, it fails with `destroy it first, or keep the current name`. Renaming may come later via `incus rename`.
- **`--unset`** removes the override, with the same refusal.
- Outside a git work tree: error, since there is nowhere to store the override.

### `adopt`

`nixant adopt [TARGET] [--instance NAME]`, for a moved checkout. Does not evaluate.

- **Candidates:** managed instances with `user.nixant.target=<TARGET>` whose `user.nixant.root` no longer exists.
- **Choosing one:** exactly one candidate is adopted. Several candidates without `--instance` → error listing them. If `--instance NAME` names an instance whose `user.nixant.target` differs from `TARGET`, refuse; adopt never changes the target.
- **Refusals:** refuses if the old root still exists, and if the instance's schema differs.
- **Changes made:**
  - Rewrite `project`/`root`.
  - Rewrite the `source` of every `nixant-mount-*` device to the current root.
  - Move the GC-root symlink from `<old-id>-<target>` to `<new-id>-<target>`, or drop it if missing.

Works in any state: a changed disk `source` is remounted live on a running container (verified). A stopped instance whose old source is gone cannot start until adopted ("Missing source path"). VM behavior is verified in Phase 3.

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
user.nixant.activation=pending|ok|degraded|failed|reboot-required
user.nixant.system=<store path>        # last system activated with result ok or degraded
user.nixant.user=<guest user name>     # used by shell/exec without evaluation
user.nixant.workdir=<path>
```

An instance belongs to a target only if `managed=true`, `project` equals the current project ID, **and** `target` equals the target. Checking the project alone is not enough: two targets of one project can be configured with the same `instanceName`. Errors:

```text
error: instance foo-dev exists but is not managed by nixant
error: instance foo-dev belongs to /other/checkout; for a second checkout run `nixant name dev`, or `nixant adopt` if that checkout was moved
error: instance foo-dev belongs to target test of this project; give dev a different nixant.instanceName
```

Tool-managed devices are prefixed `nixant-` (e.g. `nixant-mount-workspace`, `nixant-port-8080`). Reconciliation only adds, changes, or removes prefixed devices. Single exception: the instance-local `root` disk device, and only its `size` key (see Reconciliation). Profiles are never modified.

No local state file. Snapshots (Phase 4) capture the keys with the instance.

### Schema version

`user.nixant.schema` describes the metadata layout and the device naming. When it differs from the CLI's schema:

- `up`, `rebuild`, `shell`, `exec`, `adopt` refuse: `instance foo-dev uses nixant schema 1, this CLI uses 2; destroy and recreate it, or use a matching nixant version`.
- `down`, `restart`, `destroy` proceed. They only need `managed`/`project`/`target`, which every schema keeps, so cleanup is never blocked.
- `status` shows the mismatch.

A higher instance schema than the CLI's (a downgraded CLI) is treated the same way.

This is separate from `nixant.runtime.schemaVersion`, which describes the Nix-side JSON and is checked on every evaluation.

### Instance lookup

**Metadata form** (`shell`, `exec`, `down`, `restart`, `destroy`, `status`, no evaluation): `incus list local: user.nixant.project=<id> user.nixant.target=<target> --format json`.

- 0 matches → `dev: not created` (`destroy`: note, exit 0; others: `environment does not exist; run nixant up`).
- 1 match → that instance.
- More than 1 (e.g. `incus copy`, or an old instance left after `instanceName` changed) → error listing the names, suggesting `incus delete local:NAME` for the stale one.

**Evaluating form** (`up`, `rebuild`): run the metadata lookup, then compare with the effective name `N` (override or `nixant.instanceName`). Setting or removing an override while an instance exists lands in the `X ≠ N` row; its message names both sources.

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
    def run(
        self,
        name: str,
        argv: list[str],
        *,
        user: str | None = None,
        cwd: str | None = None,
        stdin: IO[bytes] | None = None,
        capture: bool = False,
    ) -> CompletedProcess: ...
    def exec_argv(
        self, name: str, argv: list[str], *, user: str, cwd: str
    ) -> list[str]: ...
    def find(self, metadata: dict[str, str]) -> list[MachineState]: ...
```

`exec_argv` returns the argv for `os.execvp` (shell/exec); `run` is used for readiness and activation. Planning lives in `planner.py`, not in the provider.

Command construction is confined to `providers/incus.py`:

- Always qualify instances as `local:NAME`; use the current Incus project.
- Image: `images:nixos/unstable` (only bootstraps the guest; the flake pins the real system). `images:nixos/26.05` also exists, but pinning it changes nothing: both are rebuilt daily.
  - Image churn is accepted. With Incus defaults, a cached remote image auto-updates (about 280 MB per refresh) and expires 10 days after it was last used.
  - These are host-wide settings (`images.auto_update_cached`, `images.auto_update_interval`, `images.remote_cache_expiry`) shared with unrelated instances, so nixant never changes server config. The README documents them for admins who want less churn.
- `security.nesting=true` is always set: guest-side Nix builds need it (see Guest-side Nix).
- Create: `incus create images:nixos/unstable local:NAME -c security.nesting=true -c user.nixant.*…`; add devices; then `incus start local:NAME`. The image is named with its `images:` remote and the destination with `local:`. A bare `local:IMAGE` would search the local image store instead.
- VM (Phase 3): `--vm -c security.secureboot=false` (image declares `requirements.secureboot=false`).
- Mounts (containers): `disk source=<abs> path=<target> shift=true [readonly=true]`. If adding with `shift=true` fails, stop with an error. Without shift, files appear as 65534 and are not writable. Verify the mount appeared and retry (see Relation to Incant).
- Ports: `proxy listen=tcp:<address>:<host> connect=tcp:127.0.0.1:<guest>`.
- Inspect via `incus query /1.0/instances/NAME?recursion=1` (status, type, config, devices, and `state.network` for IPv4 in one call; verified that the single-instance GET with `recursion=1` includes `state`).
- Lookup via `incus list local: user.nixant.project=<id> user.nixant.target=<t> --format json` (filters AND together; verified).

## Activation

```text
set user.nixant.activation=pending
paths   = nix-store -qR <toplevel>                                  (host)
missing = run(guest, ["nix-store","--check-validity","--print-invalid", *paths])
nix-store --export <missing> | run(guest, ["nix-store","--import"], stdin=pipe)
run(guest, ["nix-env","-p","/nix/var/nix/profiles/system","--set",toplevel])
rc = run(guest, [toplevel+"/bin/switch-to-configuration","switch"])
record result (below)
```

`pending` is written first, so an interruption at any later point (including during import) leaves `pending`, never a stale `ok`. `--check-validity --print-invalid` exits 0 and prints the invalid paths (verified on Nix 2.34). Its exit status is not used as a signal.

Recording, based on the `switch-to-configuration` exit status:

| Exit | `activation` | Also set | CLI result |
|---|---|---|---|
| 0 | `ok` | `system`, `user`, `workdir` | success |
| 4 (some units failed) | `degraded` | `system`, `user`, `workdir` | warning, failed units listed, exit 0 |
| 100 (new `init` interface, effective after reboot) | `reboot-required`, then resolved by the restart below | after restart: as for 0 or 4 | note, then automatic restart |
| other (1 pre-switch check or lock held, 2 activation script, …) / import failed | `failed` | nothing else | error, exit 1 |
| interrupted (Ctrl-C, `--timeout`, lost connection) | stays `pending` | nothing else | error, exit 1 |

**Exit 100.** The new system is already installed as the boot default (container `/sbin/init`), but the running system cannot switch to it live. This is most likely on the first activation (stock image → project nixpkgs), where nothing is lost. nixant:

1. Records `reboot-required` and prints `new system needs a restart; restarting <name>`.
2. Runs `incus restart --timeout 60 local:NAME`, then readiness.
3. Compares `/run/current-system` with the built path:
   - equal, and readiness saw `running` → `ok`
   - equal, and readiness saw `degraded` → `degraded`
   - different → `failed` (`instance did not boot the new system`)
4. A failed restart leaves `reboot-required`. The skip condition treats it as not ok, so the next `up` re-runs activation, which is a no-op `switch` once the instance has rebooted.

**Interruption and `--timeout`.** No default bound: interactive use streams the output, and the user can press Ctrl-C. `switch-to-configuration-ng` waits for systemd jobs with no timeout of its own, and `Type=oneshot` units have no start timeout by default, so scripts and agents should pass `--timeout DURATION` to `up`/`rebuild`.

- **Ctrl-C or expiry** ends the `incus exec` client. That also kills the guest process (verified), so the switch is aborted midway.
- **Recovery:** the system profile already points to the new generation, and `activation` stays `pending`, so the next `up` re-runs the switch. That is safe to repeat.
- **Message:** `--timeout` expiry prints `activation did not finish within <DURATION>; aborted, run nixant up to retry`.
- **Later:** a detached switch that survives nixant exiting (a transient systemd unit, as `nixos-rebuild` does with `systemd-run`) is deferred until interruptions prove common.

**Concurrent switch in the guest.** `switch-to-configuration-ng` holds an exclusive non-blocking lock on `/run/nixos/switch-to-configuration.lock`. A second switch exits 1 with `Could not acquire lock`; nixant maps this to `another activation is running inside <name>`. The host-side target lock (see Locking) prevents this between nixant runs. The guest lock covers switches started by hand inside the guest.

Exit-code meanings are from `switch-to-configuration-ng`. Supported guest nixpkgs is 25.05 or newer: `switch-to-configuration-ng` by default, and systemd ≥ 256, which `is-system-running --wait` needs. The tool modules assert `lib.versionAtLeast lib.trivial.release "25.05"`, so an older pin fails at evaluation with a clear message instead of misreporting activation results.

Skip condition (`up` only): skip activation only if `activation=ok` **and** `system` equals the built path **and** `/run/current-system` equals the built path. `/run/current-system` alone is not evidence of success: the activation script updates it before `switch-to-configuration` finishes restarting units, so a failed switch can leave it pointing at the new system. `degraded`, `failed`, `pending`, and `reboot-required` never skip, so `up` retries them. `rebuild` never skips.

- Guest root is a trusted Nix user, so unsigned imports work. Tested with an 8-path closure on Nix 2.34.
- No flakes or `NIX_CONFIG` needed for activation; nixant never evaluates in the guest. The user's own guest-side Nix use is separate (see Guest-side Nix).
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

- At creation with `nixant.disk` set: `incus create … -d root,size=<bytes>`. This makes an instance-local override of the inherited device; `pool` is inherited from the profile automatically.
- Later changes: if `root` is still inherited, `incus config device override local:NAME root size=<bytes>`. Otherwise `incus config device set local:NAME root size=<bytes>`.
- nixant only touches the `size` key of the instance-local `root` device. It never edits the profile, which is shared with unrelated instances.
- `nixant.disk = null` leaves the root device alone. An existing override is not removed.
- Pools without quota support: before applying, read the root pool's driver (`incus storage show <expanded_devices.root.pool>`). If the driver cannot enforce a size limit (`dir`), fail with `nixant.disk is not supported on storage pool <pool> (driver dir); unset it or use a btrfs/zfs/lvm pool`. Never silently ignore the setting. Exact per-driver support to be verified in Phase 2.

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
  project.py               # discovery, project ID, instance-name override lookup, mount path resolution, untracked pre-flight, locking
  init.py                  # template listing, nix flake init, instance-name proposal, lock pinning, snippets
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

## Packaging

- **Distribution: Nix only.** `nix profile install github:…/nixant` or `nix run github:…/nixant -- up`. No PyPI release.
- **Python ≥ 3.12.** `pyproject.toml` with the `hatchling` build backend and `[project.scripts] nixant = "nixant.cli:app"`. Runtime dependency: `typer`. Dev-only: `pytest`, `ruff`, `mypy`.
- **Nix package.** The nixant flake exposes `packages.x86_64-linux.default` and `apps.default`, built with `python3Packages.buildPythonApplication { pyproject = true; build-system = [ hatchling ]; dependencies = [ typer ]; }`.
- **Baked-in source** (used by `init`). `makeWrapperArgs` sets:
  - `NIXANT_SELF=${self}`: the flake's own source store path, from which `init` copies templates and snippets.
  - `NIXANT_REV=${self.rev or ""}`: used to pin the project's lock at birth.

  Unset `NIXANT_SELF` makes `init` fail with `init needs the Nix-packaged nixant`. The dev shell sets it to the checkout.
- **Host tools are not wrapped.** `incus`, `nix`, and `git` come from the host `PATH`, because the incus client must match the host daemon and nix must talk to the host's daemon. The CLI checks for them at startup and names any that are missing.
- **Dev shell.** `nix develop` provides Python with the dependencies, the dev tools, `PYTHONPATH=$PWD/src`, and `NIXANT_SELF=$PWD`.

## Relation to Incant

No Incant code is reused; everything is written fresh. Behavior carried over as knowledge only:

- Disk devices sometimes fail to appear in the guest (incus issue #1881): verify via `/proc/mounts`, remove and re-add the device on failure, bounded retries.
- VM exec before the agent is up fails with `Error: VM agent isn't currently running`; treat it as "not ready yet".
- Inspect instances with `incus query /1.0/instances/NAME?recursion=1` rather than parsing `incus list`.

---

## Phases

### Phase 1: vertical slice (containers)

Nix options module and container module, example flake, `templates.default`, discovery, untracked pre-flight, locking, eval, host build, ownership metadata and schema check, create, workspace mount, readiness, activation, `nixant.user` (with sudo), `init`, `up`, `rebuild`, `shell`, `exec`, `down`, `destroy`, `status`, `config`.

### Phase 2: machine settings

cpus/memory/disk limits, ports, multiple and read-only mounts, planner with classification, `restart`, `adopt`, `name` and the override lookup, `status --orphans`, `node` and `python` templates.

### Phase 3: VMs

`nixosModules.vm`, secure boot setting, VM readiness timeouts, VM mount/port behavior.

### Phase 4: isolation

Snapshots/restore, ephemeral instances, agent-oriented restricted profiles.

### Phase 5: second backend

Only if needed; use it to reshape `Provider`.

---

## Testing

- Unit tests, with the runner mocked: discovery, project ID, `init` name proposal and sanitization, mount resolution, untracked pre-flight (temporary git repo), runtime-JSON → dataclass mapping, ownership checks, Incus argv construction, planner classification, `exec` argv parsing, `init` with and without an existing `flake.nix`, `init` marker rewrite, `init` lock command with and without `NIXANT_REV`.
- Nix tests: `nix flake check` on the tool flake, plus `nix eval --json` of the example's `nixant.runtime` compared against a golden file. Also check that every template evaluates, and that these are rejected: path-valued mount sources, bad sizes, a missing or invalid `instanceName`, `user.name = "root"`/`uid = 0`, and nixpkgs older than 25.05.
- Unit tests, ownership and lookup (mocked `incus list`/`query` output):
  - Two targets with the same `instanceName`: the second `up` fails and the second target's `rebuild`/`destroy` never touch the first target's instance.
  - Every row of the evaluating-lookup table; 0/1/many metadata matches.
  - Ownership check rejects a project-ID match when the target differs.
- Unit tests, activation recording: `pending` written before import; exit 0 → `ok`; exit 4 → `degraded`; exit 100 → `reboot-required` → restart → `ok`/`degraded`/`failed` by the post-restart check; exits 1/2, import failure → `failed`; `Could not acquire lock` → the guest-lock message; interruption or `--timeout` expiry → `pending`; never a stale `ok`. The skip condition is false unless all three parts hold, and `rebuild` never skips.
- Unit tests, instance-name override (temporary git repos): no override → committed name; clone override; worktree override with `extensions.worktreeConfig` enabled by `nixant name`; invalid override rejected; `name` refuses when an instance exists; `status --orphans` after the root is removed.
- Unit tests, lifecycle edge cases:
  - Lock contention fails fast.
  - Schema mismatch: `up`/`shell`/`exec`/`adopt` refuse, `down`/`destroy` proceed.
  - `down` on Stopped (no `incus stop` call) and Frozen (resume, then stop).
  - `adopt` refusing a target mismatch, and moving the GC-root symlink.
  - Target-name validation, and the `workdir` fallback when the workspace mount is removed.
  - The missing-mount-source pre-check fails before any Incus call.
- Integration (gated on `NIXANT_INTEGRATION=1`; runnable in the Lima VM):
  - Happy path: temporary git repo → `init` (flake.lock pins `NIXANT_REV`) → `up`, `exec -- hostname` (equals `instanceName`), `exec -- touch /workspace/x` (host sees own UID), `exec -- sudo true`, `exec -- id -Gn` (includes `wheel`), edit config, `rebuild` (instance creation time unchanged), `down`, `up`, `status`, `destroy`.
  - Failure recovery:
    - Add a systemd service that fails → `up` reports `degraded`; a second `up` re-activates instead of skipping; fixing the service → `ok`.
    - Interrupt `up` during import → `up` again completes.
    - Introduce a Nix syntax error → `status`, `down`, `destroy` still work.
  - Cross-target: two targets with distinct names coexist; giving them the same `instanceName` makes the second `up` fail without modifying the first instance.
  - Second worktree (Phase 2): `git worktree add` → `up` fails with the `nixant name` hint → `nixant name dev` → `up` creates a second instance; both run in parallel; `git worktree remove` → `status --orphans` lists it.
  - Shell independence: set the user's shell to fish → `exec -- true` still works, and `shell` starts fish.
  - Guest-side Nix: `exec -- nix build` of a small derivation from a `/workspace` flake, and `exec -- nix develop -c true`, both as the user.
  - Stale config guard: after `up`, `exec -- sudo nixos-rebuild dry-build` fails with the nixant stub message, and `/etc/nixos/incus.nix` is gone.

## Verified during review

External simulation (flows run by hand on the Lima VM: btrfs pool, Nix 2.34, prebuilt 26.05 container system):

- **Transport is fast enough:** stdin export/import moved 1.3 GiB / 565 missing paths in about 25s, so no fallback cache is needed at this scale.
- **Activation works end to end:** setting the profile and switching took about 20s, with rc 0. After stop/start, the guest boots the activated system (`/run/current-system` is the activated path, systemd `running`), so the `up` skip condition holds across restarts.
- **Readiness is quick:** `incus exec -- true` succeeds 1–2s after start.
- **Also confirmed:** `incus list` config-key filters, `--cwd`, the `runuser` wrapper, exit-status propagation through `bash -lc 'exec "$@"'`, piping, and shifted-mount ownership.
- **First activation leaves about 2.6 GiB** in the guest store (see Deferred: guest store growth).

Own probes:

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
- `incus query /1.0/instances/NAME?recursion=1` includes `state` (with `network`); `/1.0/instances/NAME/state` is not needed separately.
- `incus list local: user.k=v user.k2=v2` filters on config keys; filters AND together.
- `incus create -d root,size=…` creates an instance-local `root` override with `pool` inherited.
- `nix-store --check-validity --print-invalid` exits 0 and prints the invalid paths.
- `incus start` resumes a Frozen container. `incus stop` on a Frozen container does not complete a clean shutdown (timed out); `--force` works. `incus stop` on a Stopped instance exits 1 (`already stopped`).
- Changing a disk device `source` on a running container remounts it live. Starting a stopped container whose disk source is missing fails with `Missing source path`.
- `nix flake lock --override-input X <url>?rev=R` records rev `R` while `original` stays the unversioned URL from `flake.nix`. Later evals and plain `nix flake lock` keep `R`; `nix flake update X` moves to latest.
- Interrupting (SIGINT/SIGTERM) or killing the `incus exec -T` client terminates the guest process.
- `switch-to-configuration-ng` source: exclusive non-blocking `flock` on `/run/nixos/switch-to-configuration.lock` (exit 1, `Could not acquire lock`). Exit 100 when the `init` interface changes. Exit 2 on activation-script failure, 4 on unit failures. `block_on_jobs` has no timeout of its own.
- Sandboxed `nix-build` as a non-root guest user through the guest daemon works with `security.nesting=true` and fails without it (`kernel namespaces that are required for sandboxing`). The container boots either way. Guest defaults: `trusted-users = root`, `substituters = https://cache.nixos.org/`, `allowed-users = *`.
- The stock image's `/etc/nixos/incus.nix` comes from the image template `nix.tpl`, which runs only on `create` and `copy`.
- Host Incus has no `images.*` overrides (defaults apply).
- `nixos-rebuild-ng` runs `switch-to-configuration` via `systemd-run --pipe --service-type=exec --unit=nixos-rebuild-switch-to-configuration` (the precedent for a later detached switch).

## To verify early

- Eval time per command after the single-evaluation change (cold ~40s and warm "seconds" were measured on a forced full evaluation, not on `runtime` + `drvPath`).
- Exactly when the activation script updates `/run/current-system` relative to unit restarts. The skip condition does not depend on the answer, but error messages might.
- That nixpkgs 25.05 is the right minimum: `switch-to-configuration-ng` is the default there and its exit codes match the recording table. The degraded-service integration test covers this on the pinned version.
- Clean `incus stop` duration for an idle NixOS container, to confirm the 60s `down` timeout is generous.
- Exit-100 path in a container: that `switch` updates `/sbin/init` before exiting 100, so `incus restart` boots the new system. Hard to trigger on purpose; try a first activation from an old stock image to current nixpkgs, otherwise cover it with the unit tests only.
- Root-disk `size` support per storage driver (Phase 2).
- VM: module name/path for the Incus VM profile, mount hotplug, proxy NAT-mode requirement.

## Deferred: personalization

Per-person settings stay out of the shared project flake, so nixant has no mechanism for them yet. Interim behavior: the shared config sets `nixant.user.uid` (default 1000). A developer whose host UID differs gets the UID-mismatch error and has to change the shared value. Personal dotfiles can only be added as team-shared home-manager config.

A git-ignored `local.nix` cannot be the answer: git flakes do not see untracked or ignored files.

Future options:

1. **CLI injects via `extendModules`.** Build `nixosConfigurations.<t>.extendModules { modules = [ { nixant.user.uid = <host uid>; nixant.user.name = <host user>; } ~/.config/nixant/user.nix ]; }` with an impure `--expr`. The UID never appears in the repo. The personal module cannot bring its own flake inputs, and the Nix eval cache is lost.
2. **Personal flake via `--override-input`.** The tool modules read an `nixant-user` input defaulting to an empty flake shipped by nixant; the CLI passes `--override-input nixant-user ~/.config/nixant` when it exists. The personal flake can have its own inputs (dotfiles, home-manager), and evaluation stays pure, but it needs more wiring and care to avoid writing the override into `flake.lock`.
3. **UID-only injection.** Option 1 restricted to `uid`/`name`; dotfiles remain a team decision.

A local instance-name override already exists for the second-checkout case (git config, see `name`). Whichever option is chosen should also make the guest hostname follow that override, which pure evaluation cannot do today.

## Deferred: guest store growth

Each activation adds a system profile generation in the guest, and old generations keep their closures alive. nixant does not collect garbage in Phases 1–2; `nixant exec -- sudo nix-collect-garbage -d` is the manual remedy. A later option is keeping the last N generations after each successful activation. On the host, only the latest build per target has a GC root.

## Non-goals

Arbitrary guest OSes, provisioners, plugin loading, remote Incus, custom networks/storage pools, image building, generic infrastructure planning, provider feature parity, Incus config passthrough, secrets management, macOS, SSH agent forwarding, git identity/credentials inside the guest.

# nixant

NixOS development environments on local [Incus](https://linuxcontainers.org/incus/).

You describe an environment as a `nixosConfiguration` in your project's flake. nixant builds it on the host, transfers the closure into an Incus container and activates it, then gives you a shell inside. The project checkout is mounted into the guest, so editors and tools on the host keep working.

> Status: containers and VMs.

## Host requirements

- Linux x86_64.
- Multi-user Nix with flakes enabled.
- Incus with the `local` remote and a default profile that provides a root disk and a NIC.
- Your user is in the `incus-admin` group. Restricted `incus`-group projects are untested and unsupported.
- The guest user's UID must equal your host UID, so the shifted workspace mount stays writable.

nixant never changes Incus server configuration.

## Project setup

Run `nixant init` in an empty project directory (`nixant init --list` shows the `default`, `node` and `python` templates). It writes `flake.nix` and `nix/dev.nix`, proposes `nixant.instanceName = "<dir>-dev"` (sanitized for Incus, with a warning if that instance already exists), locks the inputs, and stages the files in git when the directory is a work tree. If the lock step fails (for example offline), the files are kept and the exact `nix flake lock` command is printed.

The generated `nixant` input is `github:jasalt/nixant-py`, and the lock is pinned to the revision the CLI was built from, so project and CLI match. This applies to builds from a clean checkout, such as `nix run github:jasalt/nixant-py`. Dirty-tree builds and the `nix develop` shell have no revision to pin, so they point the input at their own source as a `path:` URL instead (a lock that is not portable to other machines). Setting `NIXANT_FLAKE_URL` (and `NIXANT_REV`) overrides the URL; `github:`, `gitlab:`, `sourcehut:` and `git+*://` URLs are supported, and anything else is rejected before files are written.

If `flake.nix` already exists, `init` writes nothing and prints a snippet to merge by hand. To set up a target manually, add nixant (and optionally home-manager) as inputs with `inputs.nixpkgs.follows = "nixpkgs"`, then define `nixosConfigurations.dev` using the nixant container module and these options:

```nix
nixant = {
  instanceName = "shop-dev";   # required: Incus instance name and guest hostname
  user = { name = "dev"; uid = 1000; sudo = true; };
  mounts.workspace = { source = "/abs/path/to/checkout"; target = "/workspace"; };
  # workdir defaults to the workspace target, else the user's home
};
```

Rules enforced at evaluation time:

- `instanceName` is required: at most 63 characters of `[a-z0-9-]`, no leading digit or dash, no trailing dash.
- Mount `source` is a string. A Nix path such as `./.` is rejected, because it evaluates to a store copy instead of the checkout.
- Mount targets are absolute and unique. Port numbers are in range and host ports are unique.
- `user.name = "root"` and `uid = 0` are rejected.
- The guest nixpkgs must be 26.05 or newer (see Limitations).

Everything else is ordinary NixOS and home-manager configuration, attached to `users.users.<name>` and `home-manager.users.<name>`.

## Workflow

```console
$ nixant init            # create flake.nix from the default template
$ nixant up              # build, create/start, transfer, activate (target "dev")
$ nixant shell           # login shell in the guest, in the workdir
$ nixant exec -- make    # run one command
$ nixant rebuild         # re-build and always re-activate a running instance
$ nixant status          # metadata and cached build state, no evaluation
$ nixant config          # runtime JSON, project ID, instance name, mount sources
$ nixant restart         # restart (or start) without re-activating
$ nixant down [--force]  # stop
$ nixant destroy         # delete the instance and its host GC root
$ nixant snapshot [NAME]  # snapshot (also: snapshots, restore NAME, snapshot NAME --delete)
$ nixant name [NAME]     # give this checkout its own instance name (git config)
$ nixant adopt           # attach an instance whose checkout moved
```

Every command takes an optional target name (default `dev`). `-v/--verbose` prints each external command before it runs. `up` and `rebuild` accept `--timeout 5m` to bound activation (default 30 minutes).

- `up` and `rebuild` are the only commands that evaluate Nix. `shell`, `exec`, `down`, `destroy` and `status` find the instance by its `user.nixant.*` Incus metadata, so a broken or removed configuration never blocks cleanup.
- `up` skips activation when the instance already runs the built system. `rebuild` never skips, so it is the explicit retry after a failed or degraded activation.
- `shell` and `exec` need a running instance with at least one completed activation (`ok` or `degraded`). They do not start the instance; run `nixant up` first.
- `status` reports, per instance, the Incus state, IPv4 addresses, activation result, whether the recorded system is `current` or `outdated` against the host GC root, and any schema mismatch.
- `config` evaluates the target without building and prints JSON.

## Machine settings

`nixant up` reconciles these outer settings with the instance (and `rebuild` warns when they differ):

```nix
nixant = {
  cpus = 2;                     # limits.cpu, applied live
  memory = "4GiB";              # limits.memory, applied live
  disk = "40GiB";               # root disk size, grow only
  ports = [ { host = 8080; guest = 80; } ];   # proxy to 127.0.0.1 unless `address` is set
  mounts.data = { source = "/srv/data"; target = "/data"; readOnly = true; };
};
```

- Only `nixant-` devices, the limit keys, and the `size` key of the instance's own `root` device are touched. Profiles and other devices are never modified.
- Leaving `cpus`, `memory` or `disk` as `null` keeps whatever the instance has.
- A new instance gets the `disk` size at creation, even when it is smaller than the profile's root disk. Shrinking `disk` on an existing instance is refused. A storage pool that cannot enforce quotas (driver `dir`) is refused before anything is created.
- `ephemeral = true` creates an Incus ephemeral instance: `nixant down` stops it and Incus deletes it (the next `up` builds a new one). The flag is fixed at creation; flipping it on an existing instance is refused until you destroy it.
- The `workspace` mount (project root at `/workspace`) stays in place when you add other mounts; drop it with `nixant.mounts.workspace.enable = false`.
- Mounts and ports that disappear from the configuration are removed from the instance.

## Agent isolation

`nixant.isolation = "agent"` restricts the guest for autonomous coding agents:

- no passwordless sudo, no `wheel` membership, and the user is not a trusted Nix user;
- only the `workspace` mount may be writable (other mounts must set `readOnly = true`), and no host ports are published;
- `cpus = 2` and `memory = "4GiB"` unless you set them.

Violations fail at evaluation time with a message naming the offending option. The profile does not restrict the guest's network access.

## Containers and VMs

A target is a container when it imports `nixant.nixosModules.container` and a VM when it imports `nixant.nixosModules.vm` (the stock Incus VM profile with `incus-agent` kept enabled). The kind of an existing instance cannot change; `up` refuses and asks you to destroy it first. VMs use `images:nixos/unstable` with `security.secureboot=false`, and get 180 seconds to become ready.

VM differences, verified on Incus with virtiofs mounts:

- CPU and memory limits and mounts (add, retarget, remove) apply to a running VM.
- A larger `disk` is stored immediately but the guest only sees it after `nixant restart`.
- `ports` are rejected by `up` before anything is built or created: Incus only allows NAT-mode proxies on VMs, which need a static IPv4 address on the instance NIC that nixant does not manage.

## `exec` environment caveats

`exec` runs the command through `bash -lc` as the guest user, whatever that user's interactive shell is. The login bash sources `/etc/profile`, so the NixOS environment and `/run/wrappers/bin` are on `PATH`. Settings defined only in a non-bash shell configuration, such as fish-only variables, do not apply to `exec`. `shell` starts the user's configured login shell, so bash, zsh and fish all start as login shells.

## Image churn

Instances bootstrap from `images:nixos/unstable`. The flake pins the real system, but the base image is rebuilt daily. With Incus defaults, a cached remote image auto-updates (about 280 MB per refresh) and expires 10 days after it was last used.

These settings are host-wide and shared with unrelated instances, so nixant leaves them alone. Admins who want less churn can tune:

- `images.auto_update_cached`
- `images.auto_update_interval`
- `images.remote_cache_expiry`

## Guest store garbage

Each activation adds a system profile generation in the guest, and old generations keep their closures alive. nixant does not collect garbage. Free space manually with:

```console
$ nixant exec -- sudo nix-collect-garbage -d
```

On the host, only the latest build per target has a GC root.

## Limitations

- Second checkout on the same host: the instance name comes from `nixant.instanceName`, so two checkouts of the same project collide. nixant refuses to operate on an instance owned by another checkout (`instance NAME belongs to /other/checkout`). Run `nixant name dev` in the second checkout to store its own name in that checkout's git config (per worktree in a multi-worktree repository). The override is invisible to Nix, so the guest hostname keeps the committed name.
- Moved checkouts: the instance keeps working under its old record until you run `nixant adopt` in the new location, which rewrites the recorded root and mount sources and re-registers the host GC root under the new checkout (if that registration fails, nothing is changed). Restoring a snapshot taken before the move keeps the mounts in the current checkout; a running instance is stopped for the restore and started again (a running ephemeral one is refused, since stopping deletes it). `nixant status --orphans` lists instances whose checkout no longer exists.
- Guest `nixos-rebuild switch` is not supported. After the first activation `/etc/nixos/configuration.nix` is a stub that fails with a message pointing back at `nixant rebuild`.
- nixpkgs 26.05 or newer in the guest. Instances bootstrap from `images:nixos/unstable`, and only 26.05 and unstable images exist. Activating an older release (25.05 and 25.11 were tried) hangs: its `switch-to-configuration` stops `dbus-broker` and then loses its own D-Bus connection. The Nix modules reject older pins at evaluation time.

## Development

```console
$ nix develop
$ ruff check . && ruff format --check . && mypy && pytest
$ nix flake check
```

`nix flake check` evaluates every template and `examples/basic` through their own `flake.nix` down to the system derivation. The example is evaluated with the inputs from its committed `flake.lock`, whose nixpkgs must match the tool's own lock; after `nix flake update`, refresh it with `nix flake lock --override-input nixpkgs github:NixOS/nixpkgs/<rev>` in `examples/basic`.

Integration tests need a disposable Incus environment. Plain `pytest` only collects `tests/unit`, so name the directory and opt in explicitly:

```console
$ NIXANT_INTEGRATION=1 pytest tests/integration          # everything, including VMs
$ NIXANT_INTEGRATION=1 pytest tests/integration -k "not vm"
```

They create instances named `nixit-*` and delete them afterwards. `test_init_lock.py` builds the package and locks generated projects without creating instances. `NIXANT_IT_NIXPKGS` (for example `github:NixOS/nixpkgs/nixos-26.05`) runs the scenarios against another guest nixpkgs.

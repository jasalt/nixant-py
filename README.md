# nixant

NixOS development environments on local [Incus](https://linuxcontainers.org/incus/).

You describe an environment as a `nixosConfiguration` in your project's flake. nixant builds it on the host, transfers the closure into an Incus container and activates it, then gives you a shell inside. The project checkout is mounted into the guest, so editors and tools on the host keep working.

> Status: containers and VMs. Snapshots and ephemeral instances are not implemented yet.

## Host requirements

- Linux x86_64.
- Multi-user Nix with flakes enabled.
- Incus with the `local` remote and a default profile that provides a root disk and a NIC.
- Your user is in the `incus-admin` group. Restricted `incus`-group projects are untested and unsupported.
- The guest user's UID must equal your host UID, so the shifted workspace mount stays writable.

nixant never changes Incus server configuration.

## Project setup

Run `nixant init` in an empty project directory (`nixant init --list` shows the `default`, `node` and `python` templates). It writes `flake.nix` and `nix/dev.nix`, proposes `nixant.instanceName = "<dir>-dev"` (sanitized for Incus, with a warning if that instance already exists), locks the inputs, and stages the files in git when the directory is a work tree. If the lock step fails (for example offline), the files are kept and the exact `nix flake lock` command is printed.

The generated `nixant` input points at the nixant source the CLI was built from, so project and CLI match. A package can declare a canonical URL by setting `NIXANT_FLAKE_URL` (and `NIXANT_REV`); `init` then writes that URL and pins the lock to the CLI's revision (`github:`, `gitlab:`, `sourcehut:` and `git+*://` URLs are supported; anything else is rejected before files are written).

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
$ nixant name [NAME]     # give this checkout its own instance name (git config)
$ nixant adopt           # attach an instance whose checkout moved
```

Every command takes an optional target name (default `dev`). `-v/--verbose` prints each external command before it runs. `up` and `rebuild` accept `--timeout 5m` to bound activation.

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
- Shrinking `disk` is refused. A storage pool that cannot enforce quotas (driver `dir`) is refused before anything is created.
- Mounts and ports that disappear from the configuration are removed from the instance.

## Containers and VMs

A target is a container when it imports `nixant.nixosModules.container` and a VM when it imports `nixant.nixosModules.vm` (the stock Incus VM profile with `incus-agent` kept enabled). The kind of an existing instance cannot change; `up` refuses and asks you to destroy it first. VMs use `images:nixos/unstable` with `security.secureboot=false`, and get 180 seconds to become ready.

VM differences, verified on Incus with virtiofs mounts:

- CPU and memory limits and mounts (add, retarget, remove) apply to a running VM.
- A larger `disk` is stored immediately but the guest only sees it after `nixant restart`.
- `ports` are rejected: Incus only allows NAT-mode proxies on VMs, which need a static IPv4 address on the instance NIC that nixant does not manage.

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
- Moved checkouts: the instance keeps working under its old record until you run `nixant adopt` in the new location, which rewrites the recorded root and mount sources. `nixant status --orphans` lists instances whose checkout no longer exists.
- Guest `nixos-rebuild switch` is not supported. After the first activation `/etc/nixos/configuration.nix` is a stub that fails with a message pointing back at `nixant rebuild`.
- nixpkgs 26.05 or newer in the guest. Instances bootstrap from `images:nixos/unstable`, and only 26.05 and unstable images exist. Activating an older release (25.05 and 25.11 were tried) hangs: its `switch-to-configuration` stops `dbus-broker` and then loses its own D-Bus connection. The Nix modules reject older pins at evaluation time.

## Development

```console
$ nix develop
$ ruff check . && ruff format --check . && mypy && pytest
$ nix flake check
```

Integration tests need a disposable Incus environment and run with `NIXANT_INTEGRATION=1`.

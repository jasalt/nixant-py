# nixant

NixOS development environments on local [Incus](https://linuxcontainers.org/incus/).

You describe an environment as a `nixosConfiguration` in your project's flake. nixant builds it on the host, transfers the closure into an Incus container and activates it, then gives you a shell inside. The project checkout is mounted into the guest, so editors and tools on the host keep working.

> Status: Phase 1 (containers). `init`, `restart`, `name` and `adopt` are not implemented yet; the commands below are the ones that exist.

## Host requirements

- Linux x86_64.
- Multi-user Nix with flakes enabled.
- Incus with the `local` remote and a default profile that provides a root disk and a NIC.
- Your user is in the `incus-admin` group. Restricted `incus`-group projects are untested and unsupported.
- The guest user's UID must equal your host UID, so the shifted workspace mount stays writable.

nixant never changes Incus server configuration.

## Project setup

Until `nixant init` exists, add a target to your project's `flake.nix` by hand. Add nixant (and optionally home-manager) as inputs with `inputs.nixpkgs.follows = "nixpkgs"`, then define `nixosConfigurations.dev` using the nixant container module and these options:

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
- The guest nixpkgs must be 25.05 or newer.

Everything else is ordinary NixOS and home-manager configuration, attached to `users.users.<name>` and `home-manager.users.<name>`.

## Workflow

```console
$ nixant up              # build, create/start, transfer, activate (target "dev")
$ nixant shell           # login shell in the guest, in the workdir
$ nixant exec -- make    # run one command
$ nixant rebuild         # re-build and always re-activate a running instance
$ nixant status          # metadata and cached build state, no evaluation
$ nixant config          # runtime JSON, project ID, instance name, mount sources
$ nixant down [--force]  # stop
$ nixant destroy         # delete the instance and its host GC root
```

Every command takes an optional target name (default `dev`). `-v/--verbose` prints each external command before it runs. `up` and `rebuild` accept `--timeout 5m` to bound activation.

- `up` and `rebuild` are the only commands that evaluate Nix. `shell`, `exec`, `down`, `destroy` and `status` find the instance by its `user.nixant.*` Incus metadata, so a broken or removed configuration never blocks cleanup.
- `up` skips activation when the instance already runs the built system. `rebuild` never skips, so it is the explicit retry after a failed or degraded activation.
- `shell` and `exec` need a running instance with at least one completed activation (`ok` or `degraded`). They do not start the instance; run `nixant up` first.
- `status` reports, per instance, the Incus state, IPv4 addresses, activation result, whether the recorded system is `current` or `outdated` against the host GC root, and any schema mismatch.
- `config` evaluates the target without building and prints JSON.

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

- Second checkout on the same host: the instance name comes from `nixant.instanceName`, so two checkouts of the same project collide. nixant refuses to operate on an instance owned by another checkout (`instance NAME belongs to /other/checkout`). Give the second checkout a different `instanceName` until `nixant name` exists.
- Moving or renaming the checkout does not change the instance, but its recorded root path stays stale until `nixant adopt` exists.
- Guest `nixos-rebuild switch` is not supported. After the first activation `/etc/nixos/configuration.nix` is a stub that fails with a message pointing back at `nixant rebuild`.
- Phase 1 supports containers only. `cpus`, `memory`, `disk` and `ports` options are reserved for Phase 2.
- nixpkgs 25.05 or newer in the guest: `switch-to-configuration-ng` and systemd 256+ are required for activation results to be reported correctly.

## Development

```console
$ nix develop
$ ruff check . && ruff format --check . && mypy && pytest
$ nix flake check
```

Integration tests need a disposable Incus environment and run with `NIXANT_INTEGRATION=1`.

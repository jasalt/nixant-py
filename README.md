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
- Incus must support idmapped mounts, which the shifted workspace mount (`shift=true`) needs. On GitHub-hosted Ubuntu 24.04 runners, Ubuntu's own Incus 6.0.0 refuses the mount (`idmapping abilities are required but aren't supported on system`) while upstream's [Zabbly packages](https://github.com/zabbly/incus) (Incus 7) work. [nixant-wp-demo's workflow](https://github.com/jasalt/nixant-wp-demo/blob/master/.github/workflows/static.yml) is a working GitHub Actions setup; it also removes the root-owned `~/.config/incus` that `sudo incus admin init` leaves there.

nixant never changes Incus server configuration.

`nixant doctor` checks these requirements without a project or a Nix evaluation: platform, `incus`/`nix`/`git` on `PATH`, `incus-admin` membership, a reachable Incus daemon, idmapped mounts when Incus reports them, the default profile's root disk and NIC, multi-user Nix and flakes. It warns about what only optional features need (`caddy` for `proxy`, a Wayland session for `wayland`, a render node for `gpu`), and exits non-zero when a requirement fails.

## Running nixant

Nix builds the CLI, so the host needs no Python environment:

```console
$ nix run github:jasalt/nixant-py -- init     # a published revision
$ nix profile install github:jasalt/nixant-py # or install `nixant` once
```

From a local checkout, use a `path:` reference:

```console
$ mkdir ~/shop && cd ~/shop && git init
$ nix run path:/path/to/nixant -- init
$ nix run path:/path/to/nixant -- up
```

A `path:` build has no git revision, so the generated project points at that build's source and only works on this machine (`init` warns). A plain `/path/to/nixant` from a clean tree instead pins the project to that commit on GitHub, which fails until the commit is pushed. A project keeps the nixant snapshot it was locked to; move it to newer local changes with `nix flake lock --override-input nixant path:/path/to/nixant`.

## Project setup

Run `nixant init` in an empty project directory (`nixant init --list` shows the `default`, `node`, `python`, `devenv` and `wordpress` templates). It writes `flake.nix` and `nix/dev.nix` (the `devenv` template also a starter `devenv.nix`, `devenv.yaml` and `.gitignore`; the `wordpress` template `nix/site.nix` and `.gitignore` instead of `nix/dev.nix`), proposes `nixant.instanceName = "<dir>-dev"` (sanitized for Incus, with a warning if that instance already exists), locks the inputs, and stages the files in git when the directory is a work tree. If the lock step fails (for example offline), the files are kept and the exact `nix flake lock` command is printed.

The generated `nixant` input is `github:jasalt/nixant-py`, and the lock is pinned to the revision the CLI was built from, so project and CLI match. This applies to builds from a clean checkout, such as `nix run github:jasalt/nixant-py`. Dirty-tree builds and the `nix develop` shell have no revision to pin, so they point the input at their own source as a `path:` URL instead; that project only evaluates on this machine, and `init` warns about it. On such builds, setting `NIXANT_FLAKE_URL` (and `NIXANT_REV`) overrides the URL (a clean build always uses its own); `github:`, `gitlab:`, `sourcehut:` and `git+*://` URLs are supported, and anything else is rejected before files are written.

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

## Extension modules

For authoring and consuming extensions in separate repositories, see [External extension development](docs/extensions.md): repository trade-offs, a minimal module and template, local checkout overrides, testing and releases.

Other NixOS modules can build on nixant, for example a module that sets up a web stack and reads the project's user and forwarded ports. The flake exports these modules:

- `nixosModules.container` and `nixosModules.vm` are what a project imports; each pulls in the options and creates the guest user.
- `nixosModules.options` declares only the `nixant.*` options and their rules, without the guest user, boot or Incus settings. Use it to evaluate or test an extension module without an Incus guest; the test then has to define the `users.users` entry for `nixant.user.name` itself.
- `nixosModules.devenv` adds [devenv](https://devenv.sh) and git to the guest and configures the devenv binary cache in the guest's Nix daemon, so it also applies under `isolation = "agent"`, where the user is not a trusted Nix user. Import it next to `nixosModules.container`. devenv then builds each project's environment inside the guest, with its own `devenv.lock`; nixant does not start devenv processes. `nixant init devenv` starts a project with it, and [`examples/devenv-wordpress`](examples/devenv-wordpress/README.md) runs a WordPress stack this way.
- `nixosModules.wordpress` is the WordPress extension, see below.

Extension modules may read these options. The container and VM modules set `nixant.enable = true`; with the options module alone, set it yourself, otherwise the workspace mount default does not exist:

| Option | Meaning |
|---|---|
| `nixant.user.name`, `nixant.user.uid` | The guest user, whose UID equals the host UID. Services that must read and write the workspace run as this user. |
| `nixant.mounts.<name>.{source,target,readOnly,enable}` | Host mounts; `nixant.mounts.workspace.target` (default `/workspace`) is the project root in the guest. |
| `nixant.ports` | List of `{ host; guest; address; hostname; }` forwards; `address` defaults to `127.0.0.1`. An optional unique `hostname` (e.g. `mysite.localhost`) is recorded by `up` as `user.nixant.routes` for a host-side proxy. |
| `nixant.isolation` | `"none"` or `"agent"` (see Agent isolation). |
| `nixant.instanceName`, `nixant.workdir`, `nixant.cpus`, `nixant.memory`, `nixant.disk`, `nixant.ephemeral` | Instance settings. |

`nixant.runtime` is internal (the JSON the CLI consumes) and read-only; extension modules should not read or set it. To check that nixant is present, test `options ? nixant` in the module arguments, and read `config.nixant` only behind that.

[`extensions/wordpress`](extensions/wordpress/README.md) is such an extension, kept in its own directory and exported by this flake as `nixosModules.wordpress` and `templates.wordpress` (`nixant init wordpress`): a WordPress module and template that runs MariaDB, PHP-FPM, Caddy and Mailpit as native NixOS services, built on the host and set up by `nixant up`, with the whole site in a project directory shared with the guest. [nixant-wp-demo](https://github.com/jasalt/nixant-wp-demo) is a complete site built with it. Neither uses devenv; they are separate from `nixosModules.devenv` and [`examples/devenv-wordpress`](examples/devenv-wordpress/README.md), which only demonstrates the devenv layer with a WordPress stack.

## Workflow

```console
$ nixant init            # create flake.nix from the default template
$ nixant up              # build, create/start, transfer, activate (target "dev")
$ nixant up --dry-run    # build, then show instance changes and the package diff; apply nothing
$ nixant shell           # login shell in the guest, in the matching directory (alias: ssh)
$ nixant exec -- make    # run one command
$ nixant logs [-f]       # guest journal; NAME... picks units or nixant.logs files
$ nixant forward 3000    # 127.0.0.1:3000 to guest port 3000 until Ctrl-C (also: forward GUEST HOST)
$ nixant rebuild         # re-build and always re-activate a running instance (alias: reload)
$ nixant status          # metadata and cached build state, no evaluation
$ nixant config          # runtime JSON, project ID, instance name, mount sources
$ nixant restart         # restart (or start) without re-activating
$ nixant down [--force]  # stop
$ nixant destroy         # delete the instance and its host GC root (asks first; -y skips, and is required without a terminal)
$ nixant snapshot [NAME]  # snapshot (also: snapshots, restore NAME, snapshot NAME --delete)
$ nixant name [NAME]     # give this checkout its own instance name (git config)
$ nixant adopt           # attach an instance whose checkout moved
```

Every command takes an optional target name (default `dev`). Without one, `shell`, `exec` (`-n TARGET`), `up`, `rebuild`, `down`, `restart`, `destroy`, `config`, `snapshot`, `snapshots` and `restore` (`-t TARGET`) use the target whose mount source contains the current directory, the deepest one if mounts nest, so `nixant shell` inside `www/site` enters the instance that mounts `www/site`; they fall back to `dev` outside every mount or when several targets mount the same directory. `up` and `rebuild` find the instance the same way, so the first `up` of a site still needs its name. `shell` and `exec` start where the current directory appears in the guest, `/workspace/public_html` from `www/site/public_html`, and in the target's workdir when the current directory is outside its mounts. When the instance is stopped or frozen, `shell` and `exec` ask on a terminal whether to start it (without re-activating, like `restart`); without a terminal they fail and point to `nixant restart`. This reads the instances' Incus mount devices and does not evaluate Nix. `-v/--verbose` prints each external command before it runs. `up` and `rebuild` accept `--timeout 5m` to bound activation (default 30 minutes).

- `up` and `rebuild` are the only commands that evaluate Nix. `shell`, `exec`, `down`, `destroy` and `status` find the instance by its `user.nixant.*` Incus metadata, so a broken or removed configuration never blocks cleanup.
- `up --dry-run` evaluates and builds, then prints the instance changes `up` would make (or what a new instance would get) and the package diff against the system recorded on the instance (`nix store diff-closures`). It creates, starts, changes and activates nothing, and leaves the host GC root at the deployed system. It exits non-zero when `up` would refuse.
- `up` skips activation when the instance already runs the built system. `rebuild` never skips, so it is the explicit retry after a failed or degraded activation.
- `shell` and `exec` need a running instance with at least one completed activation (`ok` or `degraded`). They do not start the instance; run `nixant up` first.
- `status` reports, per instance, the Incus state, IPv4 addresses, activation result, whether the recorded system is `current` or `outdated` against the host GC root, and any schema mismatch.
- `config` evaluates the target without building and prints JSON.
- `forward GUEST_PORT [HOST_PORT]` adds a temporary loopback proxy device (`nixant-forward-<port>`), for a dev server started by hand, and removes it on Ctrl-C, SIGTERM or the terminal closing. Nothing goes into the configuration, and `up` leaves these devices alone. A forward killed with SIGKILL stays until `incus config device remove local:NAME nixant-forward-<port>`. Containers only, like `ports`.
- `logs` shows the guest's systemd journal as root, the last 50 lines by default (`-n`), and keeps printing with `-f`. Each `NAME` is a log file declared in `nixant.logs` (`nixant.logs.debug = "/workspace/log/debug.log";`), which `tail` shows, or else a systemd unit (`nixant logs nginx phpfpm-wordpress -f`). Files and units can be mixed. `up` records the declared files on the instance, so `logs` does not evaluate Nix.

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
- The `workspace` mount (project root at `/workspace`) stays in place when you add other mounts; drop it with `nixant.mounts.workspace.enable = false`, or override single keys such as `nixant.mounts.workspace.target = "/code"` or `readOnly = true`.
- Mounts and ports that disappear from the configuration are removed from the instance.

## Port-less hostnames

Give a forward a `hostname` (`nixant.ports = [{ host = 8105; guest = 80; hostname = "mysite.localhost"; }]`)
and run `nixant proxy` on the host to reach it as `http://mysite.localhost`.
The proxy needs `caddy` on `PATH`, serves only on `127.0.0.1` and `[::1]`, keeps
its admin API on a Unix socket in `$XDG_RUNTIME_DIR`, and follows the running
instances in Incus, so routes appear and vanish with `up` and `down`. Unknown
hosts get a 404, and a hostname claimed by two instances is not routed.
`--https` also serves `https://mysite.localhost` on 443 with certificates from Caddy's own CA; run `caddy trust` once so browsers accept it (`--https-port` changes the port). Port 80 needs a one-time host change that nixant never makes; `nixant proxy
--print-setup` shows the options, or pass `--port` to use another port.

Under Lima, where Incus runs in the VM but the browser on the desktop, run Caddy on the desktop with `nixant proxy --routes-from 'limactl shell default nixant proxy --routes'`; `--routes` prints the route table as JSON and the desktop polls it every few seconds. The desktop needs `caddy` and `nixant`, and Lima's forwards of the sites' ports to the desktop's loopback.

## Wayland windows

`nixant.wayland = true` shows guest windows on the host desktop. `up` adds a `nixant-wayland` proxy device that connects to the compositor socket of the session it runs in (`$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY`) and listens in the guest on `/dev/nixant-wayland-0`, owned by the guest user with mode `0600`. The proxy connects to the host as your user, not as root. The guest module links that socket to `$XDG_RUNTIME_DIR/wayland-0` and sets `WAYLAND_DISPLAY=wayland-0` and `NIXOS_OZONE_WL=1`, so Electron and Chromium use Wayland too. It also enables lingering for the user, because `shell` and `exec` open no login session that would create `/run/user/<uid>`.

- The proxy looks the socket up on each new connection, so it keeps working after you log out and in again, as long as the compositor keeps the same socket name.
- When `up` runs without `WAYLAND_DISPLAY` (for example over ssh), an existing device keeps the socket it recorded. A new one gets `/run/user/<uid>/wayland-0` and `up` prints a warning.
- Containers only. `up` refuses `wayland` on a VM, because Incus cannot proxy a Unix socket into one.
- Audio is not forwarded. The same mechanism would work for it: a second proxy device for the host's `pipewire-0` or `pulse/native` socket, linked into the guest's runtime directory. Sharing it also gives the guest the microphone.

Two further settings are off by default:

```nix
nixant = {
  wayland = true;
  x11 = true;   # X11-only apps, through xwayland-satellite in the guest
  gpu = true;   # hardware rendering on the host's GPU
};
```

- `x11` runs [xwayland-satellite](https://github.com/Supreeeme/xwayland-satellite) as a user service in the guest and sets `DISPLAY=:0`. X11 apps become ordinary Wayland windows, and the host's X server is never shared. It requires `wayland`. The service restarts until the compositor is reachable again, so it survives a restart of the host session. It adds Xwayland to the guest closure.
- `gpu` shares the host's first render node (`/dev/dri/renderD*`) as a `nixant-gpu` device owned by the guest's `render` group, adds the user to that group, and enables `hardware.graphics`, so Mesa from the guest's nixpkgs drives the GPU. The display (`card`) nodes are not shared. Containers only; `up` refuses it before building when the host has no render node.
- Without `gpu`, apps render in software (shared memory, or llvmpipe for OpenGL). That is fine for terminals and dialogs, but browsers, Electron apps and video playback then load the CPU.

The socket and the render node both give guest code a path into the host; see [security.md](docs/security.md#7-the-wayland-socket-reaches-the-host-desktop) and [the GPU section](docs/security.md#8-a-shared-gpu-exposes-the-host-kernel-driver).

## Agent isolation

`nixant.isolation = "agent"` restricts the guest for autonomous coding agents:

- no passwordless sudo, no `wheel` membership, and the user is not a trusted Nix user;
- only the `workspace` mount may be writable (other mounts must set `readOnly = true`), and host ports may only be forwarded on `127.0.0.1` (the default `address`), so a service in the guest can be reached from the host but not from the network;
- `cpus = 2` and `memory = "4GiB"` unless you set them.

Violations fail at evaluation time with a message naming the offending option. The profile does not restrict the guest's network access.

The profile reduces the guest's privileges; it is not a sandbox for hostile code. `/workspace` stays writable, including `.git/hooks`, `.git/config` (for example `core.hooksPath` or `core.fsmonitor`) and `.envrc`, and host tools such as git, direnv, editors and nixant's own `git` calls run them as your host user. Review guest-written changes to those files before running host tools in the checkout. [security.md](security.md) describes these and other limits and a hardening order.

Switching `isolation` back from `"agent"` keeps the 2 CPU and 4 GiB caps on an existing instance, since `null` limits are left alone (see Machine settings). Set `cpus` and `memory` explicitly, or remove the caps with `incus config unset local:NAME limits.cpu limits.memory`.

## Containers and VMs

A target is a container when it imports `nixant.nixosModules.container` and a VM when it imports `nixant.nixosModules.vm` (the stock Incus VM profile with `incus-agent` kept enabled). The kind of an existing instance cannot change; `up` refuses and asks you to destroy it first. VMs use `images:nixos/unstable` with `security.secureboot=false`, and get 180 seconds to become ready.

VM differences, verified on Incus with virtiofs mounts:

- CPU and memory limits and mounts (add, retarget, remove) apply to a running VM.
- A larger `disk` is stored immediately but the guest only sees it after `nixant restart`.
- `ports` are rejected by `up` before anything is built or created: Incus only allows NAT-mode proxies on VMs, which need a static IPv4 address on the instance NIC that nixant does not manage.
- `wayland` and `gpu` are rejected the same way: Incus proxies on VMs carry only TCP and UDP, and a render node can only be shared with a container.

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

- Second checkout on the same host: the instance name comes from `nixant.instanceName`, so two checkouts of the same project collide. nixant refuses to operate on an instance owned by another checkout (`instance NAME belongs to /other/checkout`). Run `nixant name dev` in the second checkout to store its own name in that checkout's git config (per worktree in a multi-worktree repository). Only the repository's own config counts: a value in `~/.gitconfig` or system config is ignored with a warning. The override is invisible to Nix, so the guest hostname keeps the committed name.
- Moved checkouts: the instance keeps working under its old record until you run `nixant adopt` in the new location, which rewrites the recorded root and mount sources and re-registers the host GC root under the new checkout (if that registration fails, nothing is changed). Restoring a snapshot taken before the move keeps the mounts in the current checkout; a running instance is stopped for the restore and started again (a running ephemeral one is refused, since stopping deletes it). `nixant status --orphans` lists instances whose checkout no longer exists.
- Guest `nixos-rebuild switch` is not supported. After the first activation `/etc/nixos/configuration.nix` is a stub that fails with a message pointing back at `nixant rebuild`.
- nixpkgs 26.05 or newer in the guest. Instances bootstrap from `images:nixos/unstable`, and only 26.05 and unstable images exist. Activating an older release (25.05 and 25.11 were tried) hangs: its `switch-to-configuration` stops `dbus-broker` and then loses its own D-Bus connection. The Nix modules reject older pins at evaluation time.

## Development

```console
$ nix develop
$ ruff check . && ruff format --check . && mypy && pytest
$ nix flake check
```

`nix flake check` evaluates every template, `examples/basic` and `examples/devenv-wordpress` through their own `flake.nix` down to the system derivation. The examples are evaluated with the inputs from their committed `flake.lock`, whose nixpkgs must match the tool's own lock; after `nix flake update`, refresh them with `nix flake lock --override-input nixpkgs github:NixOS/nixpkgs/<rev>` in each example directory. The devenv side of `examples/devenv-wordpress` (`devenv.nix`) is not checked; it is built in the guest. It also runs the WordPress extension's evaluation tests; its VM and Incus tests are opt-in, see [its README](extensions/wordpress/README.md#tests).

Integration tests need a disposable Incus environment. Plain `pytest` only collects `tests/unit`, so name the directory and opt in explicitly:

```console
$ NIXANT_INTEGRATION=1 pytest tests/integration          # everything, including VMs
$ NIXANT_INTEGRATION=1 pytest tests/integration -k "not vm"
```

They create instances named `nixit-*` and delete them afterwards. `test_nix_eval.py` (the CLI's evaluation expression against `examples/basic` and a minimal flake) and `test_init_lock.py` (the packaged `init` lock) need only Nix and create no instances. `NIXANT_IT_NIXPKGS` (for example `github:NixOS/nixpkgs/nixos-26.05`) runs the scenarios against another guest nixpkgs.

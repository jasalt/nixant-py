# Plan: devenv layer and WordPress example

## Goal

1. Ship devenv support in nixant as a reusable NixOS module plus a `devenv` template.
2. Add `examples/devenv-wordpress/`, a single-site WordPress example built on that module, with a
   mutable WordPress root on the workspace mount that is shared between host and guest.

Inspired by `../wp-devenv-workspace/`, but modular: nixant provides the container and the devenv
layer; the example contains only WordPress specifics.

## Relation to nixant-wp

`../nixant-wp` is a separate repository with a NixOS-based WordPress setup that does not use
devenv. It is a NixOS module plus template: native MariaDB/PHP-FPM/Caddy/Mailpit units built on
the host, setup run by `nixant up`, a shared `public/` web root, existing-site import and its own
tests. It is the WordPress solution; this example is not a competitor to it.

`examples/devenv-wordpress` exists to demonstrate the devenv layer with a familiar stack. Keep the
two distinct:

| | nixant-wp | examples/devenv-wordpress |
|---|---|---|
| Purpose | WordPress environment for client projects | Demo of `nixosModules.devenv` |
| Stack declared in | NixOS module (`nix/site.nix`) | Project `devenv.nix` |
| Built | On the host, by nixant | In the guest, by devenv |
| Services start | `nixant up` (systemd units) | `devenv up -d` (manual) |
| WP setup | On every `nixant up`, converges DB settings and URL | One idempotent devenv task, creates only |
| Web root | `public/` | `wordpress/` |
| Lives in | Own repo, depends on nixant | nixant repo |

Rules:

- No dependency in either direction: the example does not import nixant-wp modules, and nixant-wp
  does not use `nixosModules.devenv`. nixant itself never depends on nixant-wp.
- Do not grow the example toward nixant-wp features (existing-site import, rewriting `wp-config.php`,
  URL convergence, multi-site). Requests for those go to nixant-wp.
- Use different default ports and instance names so both can run side by side
  (nixant-wp's template forwards 8081 and 8025).
- The example README says in its first paragraph that it demonstrates devenv in nixant, and points
  to nixant-wp for a NixOS-native WordPress setup without devenv.

### Non-goals (first version)

- Multi-site/multi-client workspaces, a cross-target port helper, `client.nix`/`site.nix` split.
- A reusable WordPress devenv module (the 22 KB `wordpress.nix` stays in wp-devenv-workspace).
- Repository layout decisions (monorepo vs. site repos, submodules, submodule checks).
- Anything nixant-wp already covers (see above).
- Production import, `.test` DNS / host-name virtual hosts, port 80 (and its sysctl).
- Automatic `devenv up` at boot, health reporting, bulk commands.
- VM targets: nixant rejects forwarded ports on VMs (`planner.py`), so the example is container-only.

## Layers

```text
nixant.nixosModules.container     lifecycle, mounts, user, resources, ports (exists)
  └── nixant.nixosModules.devenv  devenv CLI, cache config (new)
        ├── templates/devenv      generic starter project (new)
        └── examples/devenv-wordpress
                                  WordPress devenv.nix + mutable ./wordpress root (new)
```

Layering happens by importing modules, not by running templates on top of each other.
`nixant init` writes nothing when `flake.nix` already exists; it only prints a snippet.

nixant does not know about devenv processes or WordPress. `nixant up` starts the box;
`devenv up` (inside the guest) starts the application.

## A. `nixosModules.devenv`

New file `nix/modules/devenv.nix`, exported from `flake.nix` next to `container`/`vm`/`options`.
Importing the module enables it; there is no option.

Contents:

- `environment.systemPackages = [ pkgs.devenv pkgs.git ]` (and `direnv` if useful).
- System-wide cache configuration, so devenv works under `isolation = "agent"`, where the user is
  not a trusted Nix user and cannot add substituters itself:

  ```nix
  nix.settings = {
    substituters = [ "https://devenv.cachix.org" ];
    trusted-public-keys = [ "devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw=" ];
  };
  ```

  (Verify the key against devenv's docs when implementing.) Do **not** add the user to
  `trusted-users` to silence warnings.

Tests: add a case to `nix/tests/options.nix` or `templates.nix` that the module evaluates with and
without `isolation = "agent"`, and that `trusted-users` stays without the user under agent.

## B. `devenv` template

`nix/templates/devenv/`:

```text
flake.nix        # like default, modules = [ container devenv ./nix/dev.nix ]
nix/dev.nix      # stateVersion, extra packages
devenv.nix       # minimal: packages = [ pkgs.git ]; enterShell hint
devenv.yaml      # nixpkgs input only
.gitignore       # .devenv/ .devenv.flake.nix devenv.local.nix
```

`devenv.lock` is not shipped; it is created in the guest on first `devenv shell`/`up` and should be
committed by the user.

Required alongside:

- `nix/snippets/devenv.nix`: `init` errors with "has no snippet" without it.
- `flake.nix` `templates.devenv` entry: "Default container plus devenv".
- `nix/tests/templates.nix`: add `"devenv"` to `names`; assert `hasPackage "devenv" "devenv"`.
- README: template list and the "Other NixOS modules can build on nixant" section.

## C. `examples/devenv-wordpress/`

```text
flake.nix        # imports container + devenv; instanceName, ports
nix/dev.nix
ports.nix        # { http = 8090; mailpit = 8026; } plain data, imported by flake.nix and devenv.nix
devenv.nix       # php-fpm, mariadb, caddy, mailpit, wp-cli, one setup task
devenv.yaml
.gitignore       # .devenv/ .devenv.flake.nix devenv.local.nix wordpress/
README.md
wordpress/       # mutable WP root, created on first setup (not committed)
```

Ports use host = guest and differ from nixant-wp's defaults (8081, 8025). The instance name is
`devenv-wordpress-dev`. nixant's proxy connects to `127.0.0.1:<guest>` in the guest
(`planner.py`), so services bind to loopback. Fixed ports are fine in a dedicated container;
do not rely on devenv's port allocation for forwarded services (a shifted port would leave the
proxy pointing at nothing).

### devenv.nix outline

- `languages.php`: `mysqli`, `gd`, `zip`, `exif` (+ `xdebug` optional); one fpm pool.
  php-fpm runs as the dev user, so uploads and plugin installs are owned by the user. No
  www-data ownership problems.
- `services.mysql`: MariaDB on fixed `127.0.0.1:3306`, database/user `wordpress`.
- `services.caddy`: `http://:${ports.http}` with `root * ${config.devenv.root}/wordpress`,
  `php_fastcgi` to the fpm socket, `file_server`.
- `services.mailpit`: UI on `ports.mailpit`; PHP `sendmail_path` to mailpit.
- `packages = [ pkgs.wp-cli ]`.
- `tasks."wordpress:setup"` (idempotent, never destructive):
  1. `wp core download --path=wordpress --version=<pinned>` only if `wordpress/wp-load.php` is missing.
     Never `--force`.
  2. `wp config create` only if `wordpress/wp-config.php` is missing. `DB_HOST=127.0.0.1:3306`.
  3. `wp core install` only if `wp core is-installed` fails.

After setup, `wordpress/` belongs to the user: edited from the host IDE, changed by WP itself
(updates, plugins, uploads) inside the guest. The example does not converge plugins or themes.

### Usage (README)

```sh
cd examples/devenv-wordpress
nixant up
nixant exec -- devenv up -d
nixant exec -- devenv tasks run wordpress:setup
# http://127.0.0.1:8090, mail UI http://127.0.0.1:8026
```

Use `nixant exec --target <t> -- …` for other targets.

### Shared-root considerations

- **Flake evaluation copies the source.** The CLI evaluates `.#nixosConfigurations`. In a git work
  tree only tracked files are copied, so `wordpress/` and `.devenv/` (MariaDB data) must be
  gitignored. Outside git, the whole directory, including the WP tree and the database, would be
  copied into the store on every evaluation. The README should say to keep the project in git.
  If the user wants a custom theme/plugin under version control, un-ignore only that path.
- **UID.** Mounts use `shift=true`, and deploy already rejects `nixant.user.uid != host uid`, so
  host and guest see the same owner. Nothing extra is needed.
- **Run devenv only in the guest.** `.devenv/` holds guest store paths, GC roots and the MariaDB
  data dir. Running devenv on the host in the same directory conflicts. Editing files from the
  host is fine.
- **wp-config.php contains guest-side values** (`127.0.0.1:3306`, absolute `/workspace/...` paths
  if any). Host-side `wp` commands are unsupported; run `wp` through `nixant exec`.

## Runtime semantics to document

- **Builds happen in the guest.** nixant builds the NixOS system on the host. devenv evaluates and
  realises PHP/MariaDB/Caddy inside the guest, with its own `devenv.lock`, store usage and
  first-start latency. The outer `flake.lock` does not pin devenv's inputs.
- **State lives on the mount.** WordPress files and MariaDB data are under the workspace, so they
  survive `destroy` + `up`. Container snapshots do **not** include them; back up with
  `wp db export` and the `wordpress/` directory.
- **Lifecycle.** `devenv up -d` processes do not restart after a container restart; start them
  again after `nixant up`. Autostart via a systemd user unit is a later, opt-in enhancement.
- **Isolation.** The example should work under `nixant.isolation = "agent"`: workspace is the only
  writable mount, ports are loopback, no sudo. Agent defaults are 2 CPUs / 4 GiB; check that the
  first devenv build and MariaDB fit, and document overrides if not.
- **Network.** Separate containers do not imply network isolation between them.

## Testing

Static (`nix flake check`):

- devenv template evaluates, builds and passes assertions; snippet and markers present.
- `examples/devenv-wordpress` evaluates to a container with the expected ports and mounts.
  Like `examples/basic`, a committed `flake.lock` must use nixant's nixpkgs revision. Prefer
  evaluation-only tests here (no golden `runtime.json`) to keep maintenance low.

Runtime (manual first, integration test later):

- [ ] `nixant up` of the devenv template and of the example, with and without `isolation = "agent"`.
- [ ] First `devenv up` in the guest uses caches without the user being trusted.
- [ ] `wordpress:setup` is idempotent: a second run changes nothing; it never overwrites edits.
- [ ] HTTP and Mailpit reachable from the host via the forwards; WP mail shows up in Mailpit.
- [ ] A host-side edit in `wordpress/wp-content` is served immediately; guest-created files are
      host-owned.
- [ ] Site and database survive `nixant destroy` + `nixant up`.
- [ ] After a container restart, `devenv up -d` is needed again (documented).

## Work breakdown (beads)

1. `nixosModules.devenv` + tests.
2. `devenv` template + snippet + template tests + README.
3. `examples/devenv-wordpress` + README + flake check coverage.
4. Runtime validation of the checklist above (agent and non-agent).

Later (deferred):

- Multi-site workspace with devenv (multiple targets, one mount per site, cross-target port
  check). Only if there is demand beyond what nixant-wp's one-repo-per-client model covers.
- Opt-in devenv autostart as a systemd user service.
- Separate host and guest ports for WordPress, which requires URL handling in WP.

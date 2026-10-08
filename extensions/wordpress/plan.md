# nixant-wp: implementation plan

An isolated WordPress development environment per client project, built as a NixOS module on top of [nixant](../nixant). Each client repository is its own nixant project, so it gets its own Incus instance with its own MariaDB, WordPress state, Mailpit, snapshots and (optionally) agent isolation.

This plan covers the MVP: one site per instance, created and converged by `nixant up`. Production pull/push (the bvv workflow), Xdebug, `.test` hostnames and similar extras come after it.

## Goals

- `nix flake init -t <nixant-wp>` + `nixant up` gives a working WordPress at `http://localhost:<port>` and a Mailpit inbox, with no manual steps.
- Client plugins and themes are edited on the host in the client repository and served live from the mounted workspace.
- `nixant up` converges the site declaratively (core version, plugin/theme links, activation) and is safe to rerun.
- Everything the site needs is built on the host and activated by nixant; nothing runs `devenv`, `nix build` or `wp core download` inside the guest.
- Full isolation between clients: no shared database, web server, mail sink or filesystem state.

## Non-goals (MVP)

- Several sites in one instance (VVV's model). Isolation wins over density.
- Production pull/push, database import tooling, deploy keys or SSH access from the guest.
- Xdebug, Composer per component, several PHP versions, TLS, multisite, phpMyAdmin, `.test` DNS.
- Changes to nixant beyond documentation, except where noted under "nixant-side items".

## Decisions

| # | Topic | Decision |
|---|---|---|
| 1 | Repository | Separate repo `nixant-wp`; nixant stays generic. |
| 2 | Coupling | The module reads `config.nixant.user.name` and `config.nixant.ports`; the client flake imports both nixant's module and this one. `nixant-wp` does not take nixant as a runtime input. |
| 3 | Isolation | One site per nixant instance; client repo = nixant project. |
| 4 | Services | Native NixOS units: MariaDB, one PHP-FPM pool, Caddy, Mailpit. No devenv, no process-compose. |
| 5 | Identity | PHP-FPM, Caddy and the setup unit run as the nixant guest user, so the shifted `/workspace` mount is readable and writable without extra groups. |
| 6 | Database auth | MariaDB `unix_socket` auth for the nixant user (`ensureUsers`); no database password. |
| 7 | Core source | WordPress core is a Nix package (default `pkgs.wordpress`) copied into runtime state by the setup unit; no network access at runtime. |
| 8 | State | Runtime state (core copy, `wp-config.php`, uploads, database) lives in the guest under `/var/lib/wordpress` and `/var/lib/mysql`; nixant snapshots cover it. Client code lives in `/workspace`. |
| 9 | Provisioning | One idempotent `wp-site setup` script, run by `wordpress-setup.service` (oneshot) on every activation where its inputs change; a failure makes `nixant up` report `degraded`. |
| 10 | Mail | PHP `sendmail_path` → `mailpit sendmail` → Mailpit SMTP on 127.0.0.1:1025; UI on 127.0.0.1:8025, forwarded by `nixant.ports`. |
| 11 | URL | `wordpress.url` defaults to `http://localhost:<host port forwarded to guest 80>`; it must be set explicitly when no such port exists. |
| 12 | PHP | Default `phpPackage` builds on nixpkgs' `pkgs.php` (8.4 at the pin); clients override `wordpress.phpPackage` for other versions. |
| 13 | Uploads | Uploads stay in guest state with the rest of `wp-content`; no `contentDir` option in the MVP. |
| 14 | Agent access | nixant allows loopback-only `nixant.ports` under `isolation = "agent"` (nixant change), so agent-isolated clients keep the derived URL. |

Versions in the current nixant pin (nixos-unstable `151fa4e`): WordPress 7.1.2, PHP 8.4 (8.3 available), MariaDB 11.4, Mailpit 1.31.4, WP-CLI 2.12.

## Interface with nixant

nixant-wp depends on these nixant options. They become a public interface for extension modules; nixant documents them as such (see nixant-side items).

- `nixant.user.name`: runs PHP-FPM, Caddy and setup; owns `/var/lib/wordpress`.
- `nixant.ports`: `wordpress.url` and the Mailpit hint are derived from the entries whose `guest` is 80 and 8025.
- `nixant.mounts.workspace.target` (default `/workspace`): base for `plugins.<slug>.path` and `themes.<slug>.path`.
- `nixant.isolation`: when `"agent"`, nixant allows only loopback ports (Decision 14); until that nixant change lands, `wordpress.url` must be set explicitly.

The module asserts that `config.nixant` exists, with a message telling the user to import `nixant.nixosModules.container` (or `vm`).

## Repository layout

```text
nixant-wp/
  flake.nix                  # nixosModules.{wordpress,default}, templates.default, checks, packages.wp-site
  flake.lock                 # nixpkgs (+ nixant for checks only)
  nix/wordpress.nix          # the NixOS module
  nix/wp-site.nix            # writeShellApplication wrapping wp-site.sh with wp-cli, mariadb client, coreutils
  nix/wp-site.sh             # setup/check logic
  templates/default/
    flake.nix                # client flake: nixpkgs, nixant, nixant-wp, follows
    nix/site.nix             # nixant + wordpress settings for one client
    plugins/.gitkeep
  tests/
    eval.nix                 # evaluation checks used by `nix flake check`
    integration.sh           # opt-in: real nixant up against Incus
  README.md
  plan.md
```

## Module specification (`nix/wordpress.nix`)

### Options (`wordpress.*`)

| Option | Type | Default | Notes |
|---|---|---|---|
| `enable` | bool | `false` | |
| `package` | package | `pkgs.wordpress` (see open questions) | Core source; nixpkgs' package lacks bundled themes. |
| `phpPackage` | package | `pkgs.php` with `mysqli pdo_mysql gd zip exif intl` (+ `imagick` if cheap) | |
| `title` | str | `"WordPress"` | Used at first install only. |
| `url` | nullOr str | derived | `http://localhost:<port>` from `nixant.ports` (guest 80); assertion if neither set nor derivable. |
| `admin.user` / `admin.password` / `admin.email` | str | `admin` / `password` / `admin@example.test` | Development-only credentials; documented as such. |
| `plugins` | attrsOf submodule | `{}` | `path` (str, relative to workspace), `activate` (bool, default `true`). Attribute name = slug. |
| `themes` | attrsOf submodule | `{}` | `path` (str, relative to workspace). |
| `activeTheme` | nullOr str | `null` | Installed from wordpress.org if not linked or bundled. |
| `wpConfig` | attrsOf (oneOf [bool int str]) | `{ WP_DEBUG = true; WP_DEBUG_LOG = true; WP_DEBUG_DISPLAY = false; }` | Applied with `wp config set` on every setup (VVV's `wpconfig_constants`). |
| `mailpit.uiPort` / `mailpit.smtpPort` | port | `8025` / `1025` | Guest ports. |

Assertions: nixant module imported; slugs match `[A-Za-z0-9_-]+`; paths are relative and do not contain `..`; `url` is an `http://` URL; `activeTheme` is a valid slug.

### Services

- **Users and directories:** `systemd.tmpfiles.rules` create `/var/lib/wordpress` (0750, owner nixant user).
- **MariaDB:** `services.mysql = { enable = true; package = pkgs.mariadb; ensureDatabases = [ "wordpress" ]; ensureUsers = [ { name = <nixant user>; ensurePermissions."wordpress.*" = "ALL PRIVILEGES"; } ]; }`. The user authenticates through `unix_socket`; `wp-config.php` uses `DB_USER = <nixant user>`, empty password, `DB_HOST = localhost:/run/mysqld/mysqld.sock`.
- **PHP-FPM:** `services.phpfpm.pools.wordpress` with `user`/`group` = nixant user, `phpPackage`, `phpOptions` (`sendmail_path = "${pkgs.mailpit}/bin/mailpit sendmail -S 127.0.0.1:<smtpPort>"`, `upload_max_filesize`, `memory_limit`), socket owned by the nixant user, dynamic process manager with small limits.
- **Caddy:** `services.caddy = { enable = true; user/group = nixant user; globalConfig = "admin off"; virtualHosts.":80".extraConfig = root + php_fastcgi + file_server; }` serving `/var/lib/wordpress`. Verify it can still bind :80 (the unit grants `CAP_NET_BIND_SERVICE`); otherwise keep the default caddy user and add it to the nixant user's group.
- **Mailpit:** `services.mailpit.instances.wordpress = { listen = "127.0.0.1:<uiPort>"; smtp = "127.0.0.1:<smtpPort>"; }`.
- **WP-CLI:** `environment.systemPackages = [ pkgs.wp-cli wp-site ]`, and `environment.etc."wp-cli/config.yml".text = "path: /var/lib/wordpress"` with `WP_CLI_CONFIG_PATH` pointing to it, so `nixant exec -- wp plugin list` works from any directory.
- **Setup unit:** `systemd.services.wordpress-setup` — `Type = oneshot`, `RemainAfterExit = true`, `User` = nixant user, `after`/`requires` `mysql.service`, `wantedBy` `multi-user.target`, `restartTriggers` on the generated settings file, `ExecStart = wp-site setup`. Its environment comes from one generated JSON or env file (url, title, admin, plugins, themes, activeTheme, wpConfig, core path), so a configuration change reruns it during activation.

### `wp-site setup` (idempotent, `set -euo pipefail`)

1. **Core:** if `/var/lib/wordpress/.core-version` differs from the package version, rsync the core files from the package into the runtime directory, leaving `wp-config.php` and `wp-content/` alone; record the version.
2. **Config:** create `wp-config.php` with `wp config create --skip-check` if missing; then always `wp config set` `DB_*` and every `wpConfig` entry.
3. **Database ready:** wait (bounded) for `mysqladmin ping` on the socket.
4. **Install:** `wp core is-installed || wp core install --url --title --admin_*`; then converge `home`/`siteurl` to `url` and set permalinks to `/%postname%/`.
5. **Link components:** for each plugin/theme, symlink `wp-content/{plugins,themes}/<slug>` → `<workspace>/<path>`; fail on a missing source or an unexpected existing non-symlink target; remove stale symlinks that point into the workspace but are no longer declared.
6. **Activate:** activate plugins with `activate = true`; install and activate `activeTheme` if set.
7. **Report:** print the URL, admin credentials and Mailpit URL.

`wp-site check` (used by tests): core version, `is-installed`, declared plugins active, `curl` 200 on `/` and `/wp-admin/` (redirects to login), Mailpit API reachable, and a `wp eval 'wp_mail(...)'` message visible through the Mailpit API.

### Template (`templates/default`)

`flake.nix` with inputs `nixpkgs` (nixos-unstable), `nixant` and `nixant-wp`, both following `nixpkgs`, and `nixosConfigurations.dev` importing `nixant.nixosModules.container`, `nixant-wp.nixosModules.wordpress` and `./nix/site.nix`. `nix/site.nix` sets `nixant.instanceName`, `nixant.user.uid`, `nixant.ports` (8081→80, 8025→8025) and a minimal `wordpress` block. Until nixant is published, the README documents `--override-input nixant path:/path/to/nixant` for local use.

## Workflow (MVP)

```console
$ mkdir client-a && cd client-a && git init
$ nix flake init -t github:jasalt/nixant-wp && git add -A
$ $EDITOR nix/site.nix             # instanceName, uid, plugins
$ nixant up                        # http://localhost:8081, Mailpit http://localhost:8025
$ nixant exec -- wp plugin list
$ nixant snapshot before-change    # per-client safety net
$ nixant down / nixant destroy
```

## Testing

- **Evaluation (`nix flake check`):** evaluate the template with nixant pinned as a test-only input down to `system.build.toplevel.drvPath`; assert the generated pool, Caddy site, Mailpit instance and setup unit exist; check the assertion messages for a missing nixant module, bad slugs and an underivable URL.
- **NixOS VM test (optional, needs KVM):** `pkgs.testers.runNixOSTest` with nixant's options module and the WordPress module, a fake `/workspace` with a sample plugin, and `wp-site check` as the test script. This exercises the services without Incus.
- **Integration (opt-in, real Incus):** `tests/integration.sh` creates a temporary client from the template with path inputs, runs `nixant up`, then `nixant exec -- wp-site check`, edits a plugin on the host and checks the change through `curl`, reruns `nixant up` (no-op), and finally runs `nixant destroy`. Instances are named `nixwp-it-*` and always cleaned up.

## Milestones

| | Scope | Done when |
|---|---|---|
| M0 | Repo skeleton, flake outputs, empty module with options and assertions, template, eval check | `nix flake check` passes; the template evaluates to a system derivation. |
| M1 | MariaDB, PHP-FPM, Caddy, Mailpit, core copy, `wp-config`, install | `nixant up` on a fresh client serves the WordPress front page and login; rerunning `up` is a no-op. |
| M2 | Plugin/theme links and activation, `wpConfig`, URL convergence, WP-CLI ergonomics, mail | A host edit to a linked plugin is live without `up`; `wp_mail` lands in Mailpit; removing a plugin from `site.nix` removes its link on the next `up`. |
| M3 | `wp-site check`, integration script, README (setup, workflow, snapshots, isolation, limits) | The integration script passes against Incus; a second client runs in parallel without interference. |
| M4 (post-MVP) | Host CLI in the spirit of bvv: production pull (snapshot first, rsync `wp-content` with repository excludes, `wp db export` over SSH, search-replace, plugin deactivation) and push (git); Xdebug; `.test` hostnames through Incus DNS; Composer; logs | Planned separately. |

## nixant-side items

- Document the `nixant.*` NixOS options as a stable interface for extension modules, alongside the runtime schema.
- Allow loopback-only host ports under `isolation = "agent"` (Decision 14; nixant currently forbids all ports there).
- Optional, generic: let `nixant exec`/`shell` map the host working directory under a mount source to the matching guest path (bvv's `ssh` convenience).
- Optional: `nixant init --template <flake>#<name>` to use external templates such as this one with nixant's name and URL handling.

## Open questions

- WordPress core (M1): nixpkgs' `wordpress` is the upstream tarball with the bundled plugins and themes removed, so a fresh install has no theme. Either copy core from the unpacked `pkgs.wordpress.src` (plain upstream, still a host-built fixed-output fetch) or keep `pkgs.wordpress` and link a default theme from `pkgs.wordpressPackages.themes`.

Resolved: PHP default (Decision 12), uploads (Decision 13), agent access (Decision 14). Caddy's `:80` site already listens on every interface, so the bridge address needs no Caddy change if `.test` hostnames come later.

## Risks

- **Running Caddy and PHP-FPM as the guest user** is convenient for the shifted mount but unusual; verify port binding and file permissions early (M1).
- **The setup oneshot as part of activation** makes a failed WordPress step mark the whole activation `degraded`; that is intended, but its logs must be easy to reach (`nixant exec -- journalctl -u wordpress-setup`).
- **Interface drift:** nixant renaming its options breaks this module; the eval check pins nixant and catches it on update.
- **Generated runtime state** (core copy, database) is not reproducible from the repo alone; snapshots and later the pull workflow are the recovery path.

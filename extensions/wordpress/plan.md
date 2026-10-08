# nixant-wp: implementation plan

An isolated WordPress development environment per client project, built as a NixOS module on top of [nixant](../nixant). Each client repository is its own nixant project, so it gets its own Incus container with its own MariaDB, PHP-FPM, web server, Mailpit, snapshots and (optionally) agent isolation.

The whole WordPress installation (core, `wp-config.php`, `wp-content`) lives in a directory of the client repository on the host and is served from the workspace mount; the guest holds the database and the services. WordPress is mutable and manages itself; nixant-wp provides the stack, creates what is missing and keeps the database connection and URL current.

This plan covers the MVP: one site per instance, set up by `nixant up`. Production pull/push (the bvv workflow), Xdebug, `.test` hostnames and similar extras come after it.

## Goals

- `nix flake init -t <nixant-wp>` + `nixant up` gives a working WordPress at `http://localhost:<port>` and a Mailpit inbox, with no manual steps.
- Every file WordPress runs is on the host, so editors and language servers navigate core, plugins and themes; edits are live on the next request.
- Files WordPress writes (uploads, plugin and theme installs, core updates) appear on the host, owned by the host user.
- An existing site (a `wp-content` from git, or a full copy from production) can be dropped into the web root and works after a database import.
- Everything the stack needs is built on the host and activated by nixant; nothing runs `devenv` or `nix build` inside the guest.
- Full isolation between clients: no shared database, web server, mail sink or filesystem state, and no site reachable from other instances.

## Non-goals (MVP)

- Several sites in one instance (VVV's model). Isolation wins over density.
- Declarative site contents: which plugins and themes are installed or active, and the core version, are WordPress's state, not Nix options.
- Production pull/push, deploy keys or SSH access from the guest.
- Xdebug, Composer integration, several PHP versions, TLS, multisite, phpMyAdmin, `.test` DNS.
- VM guests as a supported target (see Decision 15).
- Changes to nixant beyond documentation, except where noted under "nixant-side items".

## Decisions

| # | Topic | Decision |
|---|---|---|
| 1 | Repository | Separate repo `nixant-wp`; nixant stays generic. |
| 2 | Coupling | The module reads `config.nixant.user.name`, `config.nixant.ports` and `config.nixant.mounts.workspace`; the client flake imports both nixant's module and this one. `nixant-wp` does not take nixant as a runtime input. |
| 3 | Isolation | One site per nixant instance; client repo = nixant project. |
| 4 | Services | Native NixOS units: MariaDB, one PHP-FPM pool, Caddy, Mailpit. No devenv, no process-compose. |
| 5 | Identity | PHP-FPM, Caddy and the setup unit run as the nixant guest user, so the idmapped workspace mount is readable and writable without extra groups, and files WordPress creates belong to the host user. |
| 6 | Database auth | MariaDB `unix_socket` auth for the nixant user (`ensureUsers`); no database password. |
| 7 | Web root | `wordpress.root` (default `public`), relative to the workspace mount, holds the whole installation. It goes through the workspace mount rather than its own nixant mount because `isolation = "agent"` allows only the workspace to be writable. |
| 8 | Core | `wordpress.package` (the unpacked upstream tarball of nixpkgs' WordPress) seeds the web root once, when `wp-includes` is missing, without overwriting existing files. After that WordPress owns and updates its core. |
| 9 | wp-config.php | Created once with development defaults (`WP_DEBUG`, `WP_DEBUG_LOG`, `WP_DEBUG_DISPLAY = false`, `WP_ENVIRONMENT_TYPE = local`, `AUTOMATIC_UPDATER_DISABLED`), then the user's. Setup rewrites only `DB_NAME`, `DB_USER`, `DB_PASSWORD` and `DB_HOST`, and only when they differ, so a copied production config works and an unchanged file is not touched. |
| 10 | Provisioning | One idempotent `wp-site setup` script, run by `wordpress-setup.service` (oneshot) at boot and whenever its settings change: seed core, configure, wait for the database, install if needed, keep `home`/`siteurl`. A failure makes `nixant up` report `degraded`. |
| 11 | Mail | PHP `sendmail_path` → `mailpit sendmail` → Mailpit SMTP on 127.0.0.1:1025; UI on 127.0.0.1:8025, forwarded by `nixant.ports`. A guest-only `auto_prepend_file` pre-registers a `wp_mail_from` filter (WordPress turns `$wp_filter` entries set before it loads into hooks) that replaces an invalid sender such as `wordpress@localhost` with `wordpress.mailFrom`. No file goes into the user's `wp-content`, which may be deployed elsewhere. |
| 12 | URL | `wordpress.url` defaults to `http://localhost:<host port forwarded to guest 80>`; it must be set explicitly when no such port exists. |
| 13 | PHP | Default `phpPackage` is nixpkgs' `pkgs.php` (8.4 at the pin); clients override `wordpress.phpPackage` for other versions. opcache revalidates on every request so host edits are live. |
| 14 | Network exposure | Caddy binds `wordpress.listenAddress` (default `127.0.0.1`); nixant's port proxy connects to the guest's loopback, so the site stays off the Incus bridge. Caddy also hides dotfiles anywhere in the web root (`.git`, `.env`, `.user.ini`) and `wp-content/debug.log`, since the web root is a project directory. |
| 15 | Guest type | Containers. A VM shares the web root over virtiofs, slow for WordPress's many file checks per request, and nixant forwards no ports to VMs. |
| 16 | Agent access | nixant allows loopback-only `nixant.ports` under `isolation = "agent"` (nixant change), so agent-isolated clients keep the derived URL. |
| 17 | Git | The template's `.gitignore` ignores everything under `public/` except plugins and themes the user un-ignores with `!` lines. Ignored files are also invisible to the flake, so the site never enters the Nix store. |

Versions in the current nixant pin (nixos-unstable `151fa4e`): WordPress 7.1.2, PHP 8.4 (8.3 available), MariaDB 11.4, Mailpit 1.31.4, WP-CLI 2.12.

## Interface with nixant

nixant-wp depends on these nixant options. They are a public interface for extension modules, documented in nixant.

- `nixant.user.name`: runs PHP-FPM, Caddy and setup; owns the web root's files.
- `nixant.ports`: `wordpress.url` and the Mailpit hint are derived from the entries whose `guest` is 80 and the Mailpit UI port.
- `nixant.mounts.workspace.target` (default `/workspace`) and `.enable`: the web root is `<target>/<wordpress.root>`; the module asserts the mount is enabled.
- `nixant.isolation`: under `"agent"`, nixant allows only loopback ports (Decision 16) and only the workspace writable (Decision 7).

The module asserts that `config.nixant` exists, with a message telling the user to import `nixant.nixosModules.container`.

## Repository layout

```text
nixant-wp/
  flake.nix                  # nixosModules.{wordpress,default}, templates.default, checks, packages
  flake.lock                 # nixpkgs (+ nixant for checks only)
  nix/wordpress.nix          # the NixOS module
  nix/wp-site.nix            # writeShellApplication wrapping wp-site.sh with wp-cli, mariadb client, rsync
  nix/wp-site.sh             # setup/check logic
  templates/default/
    flake.nix                # client flake: nixpkgs, nixant, nixant-wp, follows
    nix/site.nix             # nixant + wordpress settings for one client
    .gitignore               # keeps the installation in public/ out of git
  tests/
    eval.nix                 # evaluation checks used by `nix flake check`
    template.nix             # the template evaluates to a system
    vm.nix                   # NixOS VM test (packages.vm-test)
    integration.sh           # opt-in: real nixant up against Incus
  README.md
  plan.md
```

## Module specification (`nix/wordpress.nix`)

### Options (`wordpress.*`)

| Option | Type | Default | Notes |
|---|---|---|---|
| `enable` | bool | `false` | |
| `root` | str | `"public"` | Relative to the workspace mount; no `..`, not absolute, not empty. |
| `package` | package | unpacked `pkgs.wordpress.src` | Seeds a web root without `wp-includes`; nixpkgs' `wordpress` package lacks the bundled themes. |
| `phpPackage` | package | `pkgs.php` | Needs mysqli, pdo_mysql, gd, zip, exif, intl. |
| `title` | str | `"WordPress"` | First install only. |
| `listenAddress` | nullOr str | `"127.0.0.1"` | `null` listens on every interface. |
| `url` | nullOr str | derived | `http://localhost:<port>` from `nixant.ports` (guest 80); assertion if neither set nor derivable; `http://` only. |
| `admin.user` / `admin.password` / `admin.email` | str | `admin` / `password` / `admin@example.test` | First install only; development-only credentials. |
| `mailFrom` | str | `wordpress@example.test` | Replaces an invalid sender. |
| `mailpit.uiPort` / `mailpit.smtpPort` | port | `8025` / `1025` | Guest ports. |

Assertions: nixant module imported; `root` is safe; workspace mount enabled; `url` set and `http://`.

### Services

- **MariaDB:** `services.mysql` with `ensureDatabases = [ "wordpress" ]` and the nixant user granted `ALL PRIVILEGES` on it through `unix_socket`; `DB_HOST = localhost:/run/mysqld/mysqld.sock`, empty password.
- **PHP-FPM:** `services.phpfpm.pools.wordpress` as the nixant user, with `sendmail_path`, `auto_prepend_file`, upload and memory limits, and opcache revalidating on every request.
- **Caddy:** `:80` bound to `listenAddress`, `admin off`, no reload, `root` at the web root, hidden paths answered with 404, `php_fastcgi` to the pool, `file_server`.
- **Mailpit:** `services.mailpit.instances.wordpress` on 127.0.0.1.
- **WP-CLI:** built on `phpPackage` with an ini carrying the same `sendmail_path` and `auto_prepend_file`; `/etc/wp-cli/config.yml` sets `path` to the web root and `WP_CLI_CONFIG_PATH` points to it, so `nixant exec -- wp ...` works from any directory.
- **Setup unit:** `wordpress-setup.service`, oneshot, `RemainAfterExit`, as the nixant user, after and requiring `mysql.service`, `RequiresMountsFor` the web root, `restartTriggers` on the generated settings JSON (also exported as `/etc/wordpress/site.json` for manual runs), WP-CLI cache in its `CacheDirectory`.

### `wp-site setup` (idempotent)

1. **Core:** if `<root>/wp-includes/version.php` is missing, rsync the core from the package with `--ignore-existing`, so files already in the web root win.
2. **Config:** if `wp-config.php` is missing, create it and write the development defaults; then set each `DB_*` constant that differs from the guest's database.
3. **Database ready:** wait (bounded) for the socket to accept the nixant user.
4. **Install:** if `wp core is-installed` fails, `wp core install` and set permalinks to `/%postname%/` (first install only).
5. **URL:** set `home` and `siteurl` to `wordpress.url` when they differ.
6. **Report:** print the URL, admin credentials, Mailpit URL and the web root.

`wp-site check`: the site is installed; `/` returns 200 and `/wp-admin/` redirects to the login on `listenAddress`; Mailpit's API answers and a `wp eval 'wp_mail(...)'` message is visible through it.

### Template (`templates/default`)

`flake.nix` with inputs `nixpkgs` (nixos-unstable), `nixant` and `nixant-wp`, both following `nixpkgs`, and `nixosConfigurations.dev` importing `nixant.nixosModules.container`, `nixant-wp.nixosModules.wordpress` and `./nix/site.nix`. `nix/site.nix` sets `nixant.instanceName`, `nixant.user.uid`, `nixant.ports` (8081→80, 8025→8025) and `wordpress = { enable; title; root = "public"; }`. `.gitignore` as in Decision 17. Until nixant is published, the README documents `--override-input nixant path:/path/to/nixant` for local use.

## Workflow (MVP)

```console
$ mkdir client-a && cd client-a && git init
$ nix flake init -t github:jasalt/nixant-wp && git add -A
$ $EDITOR nix/site.nix               # instanceName, uid, ports
$ nixant up                          # http://localhost:8081, Mailpit http://localhost:8025, files in public/
$ nixant exec -- wp plugin install query-monitor --activate
$ $EDITOR public/wp-content/themes/my-theme/functions.php   # live on the next request
$ nixant exec -- wp db export /workspace/db.sql && nixant snapshot before-change
$ nixant down / nixant destroy       # destroy keeps public/, drops the database
```

## Testing

- **Evaluation (`nix flake check`):** evaluate a guest with nixant's container module down to `system.build.toplevel.drvPath`; check the pool, Caddy site (root, bind, hidden paths), Mailpit instance, setup unit and settings, the prepend file in both PHP-FPM and WP-CLI, and the assertion messages for a missing nixant module, an unsafe root, a disabled workspace and an underivable URL. The template evaluates the same way.
- **NixOS VM test (`nix build .#vm-test`, needs KVM):** nixant's options module, the WordPress module and a `/workspace/public/wp-content` that already holds a plugin. Checks that the core is seeded next to it, `wp-site check`, plugin activation through WP-CLI, mail from PHP-FPM with WordPress's own sender, the loopback-only listener, hidden dotfiles and error log, that rerunning setup leaves an unchanged `wp-config.php` alone, and that a foreign `DB_HOST` is reset while other files stay.
- **Integration (opt-in, real Incus):** `tests/integration.sh` creates two clients from the template with path inputs, runs `nixant up` for both in parallel, `wp-site check`, checks that core and `wp-config.php` are on the host owned by the host user, isolation between the clients, a live host edit of a plugin, that a second `nixant up` is a no-op, and destroys both. Instances are named `nixwp-it-*` and always cleaned up.

## Milestones

| | Scope | Done when |
|---|---|---|
| M0 | Repo skeleton, flake outputs, module with options and assertions, template, eval check | `nix flake check` passes; the template evaluates to a system derivation. |
| M1 | MariaDB, PHP-FPM, Caddy, Mailpit, core seeding, `wp-config`, install | `nixant up` on a fresh client serves the front page and login from `public/`; rerunning `up` is a no-op. |
| M2 | URL convergence, WP-CLI ergonomics, mail, existing sites | A host edit is live without `up`; `wp_mail` lands in Mailpit from PHP-FPM and WP-CLI; a copied production `wp-config.php` gets the guest's database. |
| M3 | `wp-site check`, VM test, integration script, README | The integration script passes against Incus; a second client runs in parallel without interference. |
| M4 (post-MVP) | Host CLI in the spirit of bvv: production pull (snapshot and `wp db export` first, rsync `public/` on the host, `wp db import`, search-replace, plugin deactivation) and push (git); Xdebug with a `/workspace` → project path mapping; `.test` hostnames through Incus DNS; Composer; logs | Planned separately. |

## nixant-side items

- Document the `nixant.*` NixOS options as a stable interface for extension modules (done).
- Allow loopback-only host ports under `isolation = "agent"` (Decision 16; done).
- Optional, generic: let `nixant exec`/`shell` map the host working directory under a mount source to the matching guest path (bvv's `ssh` convenience).
- Optional: `nixant init --template <flake>#<name>` to use external templates such as this one with nixant's name and URL handling.

## Alternatives considered

- **Declarative site, core in the guest (the first MVP, superseded).** Core was a Nix package copied into `/var/lib/wordpress` and tracked by version, with a manifest of core files for upgrades; plugins and themes were declared as `wordpress.plugins.<slug>.path` / `themes` and symlinked from the workspace, activated on every setup, with `activeTheme` installed from wordpress.org; `wpConfig` constants and the permalink structure were reapplied on every setup; a managed must-use plugin set the mail sender. It made the site's shape reproducible from `site.nix`, but most of the code went into converging state that WordPress normally owns, admin changes were undone on the next setup, third-party plugins could not be declared, and core was not visible to the host. Replaced by the shared web root.
- **Only `wp-content` on the host.** `WP_CONTENT_DIR` pointing into the workspace, core kept in the guest. Smaller change, but core stays invisible to host tooling and two roots must be served. The whole web root is simpler and covers editor navigation.
- **Editor navigation only.** If navigation were the only goal, a lighter option would be to keep the current design and point the language server at the WordPress source: for example set intelephense's `includePaths` to the Nix store copy of core, or install `php-stubs/wordpress-stubs`. The shared web root was chosen because it also gives direct host access to uploads, installed plugins and updates, and simplifies the module.
- **A separate writable nixant mount for the web root.** Rejected: `isolation = "agent"` allows only the workspace mount to be writable.
- **Symlinking `wp-content` from the workspace into a guest-side root.** Rejected: plugin paths then resolve to `/workspace/...` while WordPress expects its own paths, which breaks `plugin_basename` and similar in subtle ways.
- **Database files on the shared mount.** Rejected: host access to a live MariaDB data directory risks corruption for no benefit; `wp db export` into the project covers the need.

## Open questions

None for the MVP.

Found while implementing: WordPress's default sender `wordpress@localhost` is rejected by PHPMailer, hence `wordpress.mailFrom` and the prepended filter; opcache must revalidate on every request for host edits to be live; Caddy must bind loopback, since nixant's proxy connects there and other instances on the bridge could otherwise reach wp-admin. Caddy's `:80` site can serve `.test` hostnames later without changes when `listenAddress` is `null`.

## Risks

- **PHP in the site can write the host project.** Core and every plugin run as the guest user, who owns the workspace; under agent isolation that equals what the agent can do already, but a compromised plugin can change any project file.
- **Snapshots cover only the database.** Restoring one does not roll back `public/`; the README recommends exporting the database into the project and keeping files in git or a host backup.
- **`auto_prepend_file` can be overridden** by a site's `.user.ini` (Wordfence's firewall sets one); mail from the default sender then fails until the site sets a valid sender.
- **Running Caddy and PHP-FPM as the guest user** is unusual; port binding works through the unit's `CAP_NET_BIND_SERVICE`.
- **The setup oneshot as part of activation** makes a failed WordPress step mark the activation `degraded`; its logs are at `nixant exec -- journalctl -u wordpress-setup`.
- **Interface drift:** nixant renaming its options breaks this module; the eval check pins nixant and catches it on update.

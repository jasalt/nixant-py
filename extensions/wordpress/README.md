# WordPress on nixant

An isolated WordPress development environment per client project: nixant's `nixosModules.wordpress` and `templates.wordpress`, an extension built on [nixant's extension interface](../../README.md#extension-modules). Each client repository is its own nixant project, so it gets its own Incus container with its own MariaDB, PHP-FPM, web server and Mailpit inbox. Nothing is shared between clients.

The whole WordPress site, meaning core, `wp-config.php` and `wp-content`, lives in a directory of the project on the host (`public/` by default) and is served from there by the guest. Your editor and language server see every file WordPress runs, and anything WordPress writes, such as uploads, plugin installs and core updates, shows up on the host right away, owned by you. The guest holds only the database and the services.

WordPress manages itself the usual way: install and update plugins, themes and core in wp-admin or with WP-CLI. `nixant up` only creates what is missing (core, `wp-config.php`, the installation) and keeps the database connection and the URL current. Rerunning it is safe.

Status: MVP. One site per instance. See `plan.md` for the design and what comes later, and [nixant-wp-demo](https://github.com/jasalt/nixant-wp-demo) for a worked example: a custom theme and content model, provisioning scripts, and a CI workflow that publishes a static export to GitHub Pages.

## Requirements

- Everything nixant needs: Linux x86_64, multi-user Nix with flakes, Incus, your user in `incus-admin`.
- The `nixant` command: `nix profile install github:jasalt/nixant-py`. Without it, `nix run github:jasalt/nixant-py --` works too, but re-evaluates on every call (seconds per `nixant exec -- wp ...`).
- The guest user's UID must equal your host UID (`id -u`), so the mounted project directory stays writable and files WordPress creates are yours.
- A container, not a VM (`nixant.nixosModules.container`). The site is served from the shared directory, which is a native bind mount in a container but a much slower virtiofs share in a VM, and nixant forwards ports only to containers.

## Quick start

```console
$ mkdir client-a && cd client-a && git init
$ nixant init wordpress           # flake.nix, nix/site.nix, .gitignore and flake.lock, added to git
$ $EDITOR nix/site.nix            # uid, ports
$ nixant up
```

When `up` finishes (`... ready`):

- the site is at `http://localhost:8081` and the admin at `http://localhost:8081/wp-admin/` (user `admin`, password `password`; development only);
- the Mailpit inbox is at `http://localhost:8025`;
- `public/` holds the WordPress installation.

`nixant init wordpress` names the instance after the directory (`client-a-dev`) and adds the files to git; `flake.nix` and `nix/site.nix` must stay tracked, as for any nixant project. In a directory that already has a `flake.nix`, it writes nothing and prints the inputs and modules to add by hand.

The project has a single input besides nixpkgs: nixant, which carries both the container module and the WordPress module. To work on nixant itself, lock a project to a local checkout with `nix flake lock --override-input nixant path:/path/to/nixant`, and back to the published one with `nix flake update nixant`.

### An existing site

Put the site's files into `public/` before `nixant up`, for example a `wp-content` from git or a full copy from production. Setup keeps every file that is there and adds the core only if `public/wp-includes` is missing. An existing `wp-config.php` is kept too; only its `DB_NAME`, `DB_USER`, `DB_PASSWORD` and `DB_HOST` are pointed at the guest's database on every setup. Then import the database:

```console
$ nixant exec -- wp db import /workspace/dump.sql
$ nixant exec -- wp search-replace https://client.example http://localhost:8081 --skip-columns=guid
```

## Git

The template's `.gitignore` keeps the WordPress installation out of git (core, `wp-config.php`, uploads, third-party plugins and themes) and has commented `!` lines to track your own plugins and themes:

```gitignore
!/public/wp-content/plugins/my-plugin/
!/public/wp-content/themes/my-theme/
```

Ignored files are also invisible to the Nix flake, so the site is never copied into the Nix store. `wp-config.php` stays out of git because setup writes the guest's database settings into it.

## Configuring the site

`nix/site.nix` holds the nixant settings and a `wordpress` block:

```nix
{ ... }:
{
  system.stateVersion = "25.05";

  nixant = {
    instanceName = "client-a-dev";   # unique Incus instance name
    user.uid = 1000;                 # must equal `id -u` on the host
    ports = [
      { host = 8081; guest = 80; }       # the site; also provides wordpress.url
      { host = 8025; guest = 8025; }     # Mailpit
    ];
  };

  wordpress = {
    enable = true;
    title = "Client A";
    root = "public";
  };
}
```

For a URL without a port, add `hostname = "client-a.localhost";` to the guest-80 forward and run `nixant proxy` on the host; `wordpress.url` becomes `http://client-a.localhost`. The guest resolves that name to itself, so WP-Cron and Site Health keep working. Switching an existing site between URLs updates `home` and `siteurl`; run `wp search-replace` for URLs stored in content.

Every client needs its own `instanceName` and its own host ports.

### Options

| Option | Default | Meaning |
|---|---|---|
| `wordpress.enable` | `false` | Turn the site on. |
| `wordpress.root` | `"public"` | Web root, relative to the project root. The whole installation lives here. Must be relative and free of `..`. |
| `wordpress.package` | upstream WordPress tarball from nixpkgs | Core copied into the web root when it has no `wp-includes` yet. After that WordPress updates itself. nixpkgs' own `wordpress` package omits the bundled themes and plugins, so a fresh site would have no theme. |
| `wordpress.phpPackage` | `pkgs.php` (8.4 at the pinned nixpkgs) | PHP for PHP-FPM and WP-CLI. Override it for another version; it needs mysqli, gd, zip, exif and intl, which nixpkgs' PHP has. |
| `wordpress.title` | `"WordPress"` | Site title, used at the first install only. |
| `wordpress.listenAddress` | `"127.0.0.1"` | Guest address the site is served on. nixant's port forward connects to the guest's loopback, so the default keeps the site off the Incus bridge, where other instances could reach it. `null` listens on every interface. |
| `wordpress.url` | `http://localhost:<host port forwarded to guest 80>`, or `http://<hostname>` if that forward sets `hostname` | The URL, `http://<host>[:<port>]` without a path, kept in the `home` and `siteurl` options. Must be set explicitly when no `nixant.ports` entry has `guest = 80`. `http://` only. The port must not be one the guest uses itself (Mailpit's, 3306). |
| `wordpress.admin.{user,password,email}` | `admin` / `password` / `admin@example.test` | Administrator for the first install. Development-only credentials; they are stored in the Nix store. |
| `wordpress.mailFrom` | `wordpress@example.test` | Sender for mail whose own sender is not a valid address. WordPress's default `wordpress@localhost` is rejected as invalid, so mail would never reach Mailpit. |
| `wordpress.mailpit.{uiPort,smtpPort}` | `8025` / `1025` | Guest ports of Mailpit. Forward `uiPort` with `nixant.ports` to read the inbox on the host. |

## Working on the site

Edit anything under `public/` on the host; changes are live on the next request, with no `nixant up`. PHP's opcache revalidates on every request for this reason.

```console
$ nixant exec -- wp plugin install query-monitor --activate
$ nixant exec -- wp core update
$ nixant exec -- wp-site check       # health check, see below
$ nixant exec -- journalctl -u wordpress-setup
$ nixant shell
```

`nixant exec -- wp ...` runs as the guest user with the web root preconfigured, from any directory.

A fresh `wp-config.php` starts with `WP_DEBUG` and `WP_DEBUG_LOG` on, `WP_DEBUG_DISPLAY` off, `WP_ENVIRONMENT_TYPE` set to `local` and background updates off. It is yours to edit afterwards; setup only rewrites the four `DB_*` constants, and only when they differ. PHP errors go to `public/wp-content/debug.log`.

Paths inside the guest start with `/workspace/` (for example `/workspace/public/wp-content/...` in a stack trace), which is the project root on the host.

Changing `site.nix` and running `nixant up`:

- changing `wordpress.url`, for example after changing the forwarded host port, updates `home` and `siteurl` (run `wp search-replace` for URLs inside content);
- changing `title`, the admin settings or `wordpress.package` does not touch an installed site.

### Health check

`wp-site check` verifies, and stops with `wp-site: check failed: <name>: <reason>` at the first failure:

1. WordPress reports an installation;
2. `/` returns 200 and `/wp-admin/` redirects to the login;
3. WordPress can request its own URL (`wp_remote_get( home_url() )` returns 200);
4. Mailpit's API answers, and a message sent with `wp_mail` shows up in it.

### Requests to the site's own URL

WordPress requests its own URL from inside the guest: WP-Cron is spawned that way, Site Health tests it, and static exporters such as Simply Static fetch every page through it. nixant forwards the host port (8081) to guest port 80, so the guest also serves the site on the URL's port, on loopback only, and maps a custom URL host (for example `http://client.test:8081`) to `127.0.0.1` in `/etc/hosts`. `wp-site check` tests this.

### Mail

PHP's `sendmail_path` points at `mailpit sendmail`, for PHP-FPM and for WP-CLI, so every message WordPress sends (password resets, `wp_mail`) lands in the per-client Mailpit inbox and nowhere else. A PHP `auto_prepend_file` in the guest replaces an invalid sender with `wordpress.mailFrom`; nothing is added to your `wp-content`. A site whose own `.user.ini` sets `auto_prepend_file` (Wordfence's firewall does) replaces it, and mail from the default sender then fails; set a valid sender in the site instead.

### Editor navigation without the shared web root

If editor navigation were the only goal, a lighter option would be to keep WordPress inside the guest and point the language server at the WordPress source instead, for example intelephense's `includePaths` at the Nix store copy of core, or the `php-stubs/wordpress-stubs` package. The module shares the whole web root because it also makes the site's files, uploads and updates directly usable on the host.

## State, snapshots and logs

The site's files live in the project directory on the host. The database lives in the guest, in `/var/lib/mysql`.

```console
$ nixant exec -- wp db export /workspace/db.sql   # the database, next to the files
$ nixant snapshot before-change
$ nixant snapshots
$ nixant restore before-change
$ nixant destroy                 # deletes the instance and the database; public/ stays
```

Snapshots cover the guest, so they restore the database but not the files in `public/`. For a consistent restore point, export the database into the project and keep `public/` in git or a host backup. Snapshots are taken while MariaDB runs, so expect crash-recovery on restore, as after a power cut.

If `wordpress-setup.service` fails, `nixant up` still finishes but warns `activated with failed units`, and `nixant status` shows `degraded`. The reason is in `nixant exec -- journalctl -u wordpress-setup`. Fix the cause and run `nixant up` again.

## Agent isolation

`nixant.isolation = "agent"` works with the WordPress module. nixant allows forwarded ports on `127.0.0.1` in that mode, so the site keeps its derived URL and the agent-isolated guest is still reachable from the host browser. The site itself listens only on the guest's loopback (`wordpress.listenAddress`), so other instances on the Incus bridge, agent-isolated or not, cannot reach its wp-admin. The guest user has no sudo; the setup service and the web stack run as that user, so they do not need it.

The web root is in the workspace, which is the one mount writable under agent isolation. PHP code in the site (core, every plugin) runs as the guest user and can write the whole project directory on the host, just as the agent can. See [nixant's README](../../README.md#agent-isolation) for what the profile does and does not restrict.

## Tests

From the repository root:

```console
$ nix flake check                            # includes wordpress-eval, wordpress-template and wp-site (shellcheck)
$ nix build .#wordpress-vm-test -L           # boots a NixOS VM (needs KVM) and runs wp-site check
$ extensions/wordpress/tests/integration.sh  # real Incus: two clients in parallel, cleans up
```

The integration script builds nixant from this checkout's working tree, creates two clients from the template under the `nixwp-it-<pid>` prefix, and destroys them on every exit path. Do not edit the checkout while it runs.

## Layout

Everything lives in `extensions/wordpress/`; the root `flake.nix` exports it.

```text
nix/wordpress.nix          the NixOS module (nixosModules.wordpress)
nix/wp-site.{nix,sh}       the setup and check script, packaged with its tools (packages.wp-site)
template/                  templates.wordpress: client flake, nix/site.nix and .gitignore
tests/                     eval.nix, template.nix, vm.nix, integration.sh
plan.md                    design and decisions
```

`nix/snippets/wordpress.nix` at the repository root is what `nixant init wordpress` prints for an existing `flake.nix`.

## Limits

- One site per instance; for several clients use several projects. No multisite.
- Containers only in practice: VMs serve the shared web root slowly and get no forwarded ports.
- Plain HTTP on a forwarded localhost port; no TLS, no Xdebug, no phpMyAdmin.
- No production pull or push tooling yet; files can be synced on the host, the database with `wp db import`.
- Nothing about the site's contents is declarative: plugins, themes, their activation and the core version live in `public/` and the database.
- Only MariaDB, PHP-FPM, Caddy and Mailpit as shipped; the PHP version is one per instance.
- Caddy, PHP-FPM and the setup unit run as the nixant guest user, which is unusual but makes the shared project directory work without extra permissions.

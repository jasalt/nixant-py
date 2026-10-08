# nixant-wp

An isolated WordPress development environment per client project, built as a NixOS module on top of [nixant](../nixant). Each client repository is its own nixant project, so it gets its own Incus container with its own MariaDB, WordPress state, Mailpit inbox and snapshots. Nothing is shared between clients.

`nixant up` builds the environment on the host, activates it in the guest and converges the site: WordPress core, `wp-config.php`, the install, your plugins and themes, and the mail sink. Rerunning it is safe.

Status: MVP. One site per instance. See `plan.md` for the design and what comes later.

## Requirements

- Everything nixant needs: Linux x86_64, multi-user Nix with flakes, Incus, your user in `incus-admin`.
- nixant itself. It is not published yet, so the commands below use a local checkout (`NIXANT=/path/to/nixant`). Once published, drop the `--override-input` flags.
- The guest user's UID must equal your host UID (`id -u`), so the mounted project directory stays writable.

## Quick start

```console
$ mkdir client-a && cd client-a && git init
$ nix flake init -t path:/path/to/nixant-wp
$ $EDITOR nix/site.nix            # instanceName, uid, ports, plugins
$ git add -A
$ nix flake lock \
    --override-input nixant path:$NIXANT \
    --override-input nixant-wp path:/path/to/nixant-wp
$ git add -A
$ nix run path:$NIXANT -- up      # or `nixant up` if installed
```

When `up` finishes (`... ready`):

- the site is at `http://localhost:8081` and the admin at `http://localhost:8081/wp-admin/` (user `admin`, password `password`; development only);
- the Mailpit inbox is at `http://localhost:8025`.

`flake.nix` and `nix/site.nix` must be tracked in git, as for any nixant project. The template's `plugins/` directory is where you put plugin directories.

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
    plugins.my-plugin.path = "plugins/my-plugin";
    themes.my-theme.path = "themes/my-theme";
    activeTheme = "my-theme";
    wpConfig.WP_POST_REVISIONS = 3;
  };
}
```

### Options

| Option | Default | Meaning |
|---|---|---|
| `wordpress.enable` | `false` | Turn the site on. |
| `wordpress.package` | upstream WordPress tarball from nixpkgs | Core source, copied into `/var/lib/wordpress` by the setup unit. nixpkgs' own `wordpress` package omits the bundled themes and plugins, so a fresh site would have no theme. The core is copied again when the package's name (its version) changes; give a patched core its own name. |
| `wordpress.phpPackage` | `pkgs.php` (8.4 at the pinned nixpkgs) | PHP for PHP-FPM and WP-CLI. Override it for another version; it needs mysqli, gd, zip, exif and intl, which nixpkgs' PHP has. |
| `wordpress.title` | `"WordPress"` | Site title, used at the first install only. |
| `wordpress.listenAddress` | `"127.0.0.1"` | Guest address the site is served on. nixant's port forward connects to the guest's loopback, so the default keeps the site off the Incus bridge, where other instances could reach it. `null` listens on every interface, for example to browse a VM guest by its address. |
| `wordpress.url` | `http://localhost:<host port forwarded to guest 80>` | The public URL. Must be set explicitly when no `nixant.ports` entry has `guest = 80`. `http://` only. |
| `wordpress.admin.{user,password,email}` | `admin` / `password` / `admin@example.test` | Administrator for the first install. Development-only credentials; they are stored in the Nix store. |
| `wordpress.plugins.<slug>.path` | none | Directory (relative to the project root) linked as `wp-content/plugins/<slug>`. |
| `wordpress.plugins.<slug>.activate` | `true` | Activate the plugin on every setup. |
| `wordpress.themes.<slug>.path` | none | Directory linked as `wp-content/themes/<slug>`. |
| `wordpress.activeTheme` | `null` | Theme to activate. A theme that is neither linked nor bundled is installed from wordpress.org, the only step that needs network access. |
| `wordpress.wpConfig` | `WP_DEBUG` and `WP_DEBUG_LOG` true, `WP_DEBUG_DISPLAY` false | Constants applied with `wp config set` on every setup. Defaults are per key, so added constants keep them. |
| `wordpress.mailFrom` | `wordpress@example.test` | Sender address. WordPress's default `wordpress@localhost` is rejected as invalid, so mail would never reach Mailpit. |
| `wordpress.mailpit.{uiPort,smtpPort}` | `8025` / `1025` | Guest ports of Mailpit. Forward `uiPort` with `nixant.ports` to read the inbox on the host. |

Slugs must match `[A-Za-z0-9_-]+`, paths must be relative and free of `..`, and wpConfig names must be PHP constant names. Violations fail at evaluation time with a message naming the option.

## Working on the site

Plugin and theme directories in the project are linked into WordPress, so edits on the host are live on the next request, with no `nixant up`. (PHP's opcache is set to revalidate on every request for this reason.)

```console
$ nixant exec -- wp plugin list      # WP-CLI works from any directory
$ nixant exec -- wp-site check       # health check, see below
$ nixant exec -- journalctl -u wordpress-setup
$ nixant shell
```

`nixant exec -- wp ...` runs as the guest user with `path: /var/lib/wordpress` preconfigured. PHP errors go to `wp-content/debug.log` and not to the page.

Changing `site.nix` and running `nixant up` converges the site:

- adding or removing a plugin or theme adds or removes its link; a removed plugin is deactivated first;
- changing `wordpress.url`, for example after changing the forwarded host port, updates `home` and `siteurl`;
- changing `wordpress.wpConfig` updates `wp-config.php`;
- changing `title` or the admin settings does not touch an installed site;
- a new `wordpress.package` replaces the core files, runs the database upgrade and keeps `wp-config.php`, `wp-content` and the uploads. Files the previous core shipped and the new one does not are removed (listed in `/var/lib/wordpress/.core-files`); other files you put in the web root, such as `robots.txt`, are kept.

If nothing changed, `nixant up` does not activate at all.

### Health check

`wp-site check` verifies, and stops with `wp-site: check failed: <name>: <reason>` at the first failure:

1. the installed core is the configured one;
2. WordPress reports an installation;
3. every plugin with `activate = true` is active;
4. `/` returns 200 and `/wp-admin/` redirects to the login;
5. Mailpit's API answers, and a message sent with `wp_mail` shows up in it.

### Mail

PHP's `sendmail_path` points at `mailpit sendmail`, for PHP-FPM and for WP-CLI, so every message WordPress sends (password resets, `wp_mail`) lands in the per-client Mailpit inbox and nowhere else.

## State, snapshots and logs

Runtime state lives inside the guest: the WordPress files, `wp-config.php` and uploads in `/var/lib/wordpress`, the database in `/var/lib/mysql`. Your code stays in the project directory on the host.

```console
$ nixant snapshot before-change
$ nixant snapshots
$ nixant restore before-change   # then `nixant up` to reconcile the configuration
$ nixant destroy                 # deletes the instance and all its state
```

Snapshots cover the whole guest, database included. They are taken while MariaDB runs, so expect crash-recovery on restore, as after a power cut.

If `wordpress-setup.service` fails, `nixant up` still finishes but warns `activated with failed units`, and `nixant status` shows `degraded`. The reason is in `nixant exec -- journalctl -u wordpress-setup`. Fix the cause and run `nixant up` again.

## Agent isolation

`nixant.isolation = "agent"` works with nixant-wp. nixant allows forwarded ports on `127.0.0.1` in that mode, so the site keeps its derived URL and the agent-isolated guest is still reachable from the host browser. The site itself listens only on the guest's loopback (`wordpress.listenAddress`), so other instances on the Incus bridge, agent-isolated or not, cannot reach its wp-admin. The guest user has no sudo; the setup service and the web stack run as that user, so they do not need it. See nixant's README for what the profile does and does not restrict.

## Tests

```console
$ nix flake check                 # evaluation tests and shellcheck, fast
$ nix build .#vm-test -L          # boots a NixOS VM (needs KVM) and runs wp-site check
$ tests/integration.sh            # real Incus: two clients in parallel, cleans up
```

The integration script builds nixant from `../nixant` (override with `NIXANT_SRC`), creates two clients from the template under the `nixwp-it-<pid>` prefix, and destroys them on every exit path. Do not edit the nixant checkout while it runs.

## Layout

```text
flake.nix                  nixosModules.{wordpress,default}, templates.default, checks, packages
nix/wordpress.nix          the NixOS module (options, services, setup unit)
nix/wp-site.{nix,sh}       the setup and check script, packaged with its tools
templates/default/         client flake and nix/site.nix
tests/                     eval.nix, template.nix, vm.nix, integration.sh
plan.md                    design and decisions
```

## Limits

- One site per instance; for several clients use several projects. No multisite.
- Plain HTTP on a forwarded localhost port; no TLS, no `.test` hostnames, no Xdebug, no phpMyAdmin.
- No production pull or push, no database import tooling.
- Runtime state (core copy, database, uploads) is not reproducible from the repository; snapshots are the recovery path.
- Only MariaDB, PHP-FPM, Caddy and Mailpit as shipped; the PHP version is one per instance.
- Caddy, PHP-FPM and the setup unit run as the nixant guest user, which is unusual but makes the mounted project directory work without extra permissions.

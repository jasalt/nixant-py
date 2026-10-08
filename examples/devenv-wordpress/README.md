# devenv WordPress example

This example shows how a nixant project uses [devenv](https://devenv.sh) through
`nixant.nixosModules.devenv`: NixOS sets up the container, and the project's own `devenv.nix`
declares the application stack, which devenv builds and runs inside the guest. WordPress is
just a familiar stack to demonstrate it with.

For a WordPress development setup without devenv, where the stack is native NixOS services
built on the host and set up by `nixant up`, use [nixant-wp](../../../nixant-wp) instead. This
example stays deliberately small and does not grow its features (existing-site import,
converging `wp-config.php` or the URL, several sites).

## What runs where

| | |
|---|---|
| `flake.nix`, `nix/dev.nix` | The container: nixant module, devenv module, forwarded ports. Built on the host by `nixant up`. |
| `ports.nix` | HTTP 8090 and Mailpit 8026, read by both `flake.nix` and `devenv.nix`. |
| `devenv.nix`, `devenv.yaml` | PHP-FPM, MariaDB, Caddy, Mailpit and WP-CLI. Built and run by devenv in the guest. |
| `wordpress/` | The WordPress root (core, `wp-config.php`, `wp-content`). Created on first setup, shared with the host through the workspace mount. |

## Usage

```console
$ cd examples/devenv-wordpress
$ nixant up
$ nixant exec -- devenv up -d
$ nixant exec -- devenv tasks run wordpress:setup
```

The first `devenv up` builds the stack in the guest and takes a while. Then:

- the site is at <http://localhost:8090>, the admin at <http://localhost:8090/wp-admin/>
  (`admin` / `password`; development only);
- Mailpit shows mail sent by WordPress at <http://localhost:8026>.

Stop the services with `nixant exec -- devenv processes down`. They do not start again by
themselves after the container restarts; run `devenv up -d` again after `nixant up`.

## The WordPress root

`wordpress:setup` only creates what is missing: it downloads core if `wordpress/wp-load.php`
is absent, writes `wp-config.php` if absent, and installs WordPress if the database is empty.
It never overwrites files, so after the first run the directory is yours: edit it from the
host, and let WordPress install plugins, themes and updates as usual. Files WordPress writes are
owned by your host user, because PHP-FPM runs as the guest user, whose UID equals yours.

`wp-config.php` holds the guest's database address, so run WP-CLI in the guest's devenv
shell, where `wp` uses the same PHP settings as the site:

```console
$ nixant exec -- devenv shell -- wp --path=wordpress plugin list
```

Setup also adds `wp-content/mu-plugins/devenv-mail-from.php` if it is missing: WordPress sends
from `wordpress@localhost`, which PHPMailer rejects, so the plugin sets a valid sender.

## Rules

- **Run devenv only in the guest.** `.devenv/` holds the guest's store paths and the MariaDB
  data. Running devenv on the host in this directory conflicts with them. Editing files from
  the host is fine.
- **Keep the project in git, with `wordpress/` and `.devenv/` ignored** (see `.gitignore`).
  nixant evaluates the flake, which copies tracked files into the Nix store; outside git, the
  whole directory, including the site and the database, would be copied on every evaluation.
- **Back up the site yourself.** Both `wordpress/` and the database live in the project
  directory, so they survive `nixant destroy` and `nixant up`, but container snapshots do not
  include them. Use `nixant exec -- devenv shell -- wp --path=wordpress db export backup.sql`.
- **Ports are strict.** `devenv.yaml` sets `strict_ports: true`, so a busy port fails the start
  instead of moving the service away from the forwarded port.

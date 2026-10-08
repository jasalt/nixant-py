# NixOS VM test: the services, setup unit and `wp-site check` without Incus.
# nixant contributes only its options module; the guest user is defined here
# because nixant's container module normally creates it.
{ pkgs, nixant, wordpress }:
let
  # Prints a marker in the footer, and on ?vm-mail sends mail with WordPress's
  # own sender (wordpress@localhost), which only the prepended filter fixes.
  plugin = pkgs.writeTextDir "it-plugin.php" ''
    <?php
    /* Plugin Name: VM test plugin */
    add_action( 'wp_footer', function () { echo '<!-- vm-plugin -->'; } );
    add_action( 'init', function () {
      if ( isset( $_GET['vm-mail'] ) ) {
        echo wp_mail( 'check@example.test', 'vm fpm mail', 'body' ) ? 'sent' : 'failed';
        exit;
      }
    } );
  '';
  site = "/workspace/public";
  get = path: "curl -s -o /dev/null -w '%{http_code}' -H 'Host: localhost:8081' http://127.0.0.1${path}";
in pkgs.testers.runNixOSTest {
  name = "nixant-wp";

  nodes.machine = { ... }: {
    imports = [ nixant.nixosModules.options wordpress ];

    virtualisation.memorySize = 2048;
    system.stateVersion = "25.05";

    nixant = {
      enable = true;
      instanceName = "vm-test";
      ports = [ { host = 8081; guest = 80; } { host = 8025; guest = 8025; } ];
    };
    users.groups.dev.gid = 1000;
    users.users.dev = {
      isNormalUser = true;
      uid = 1000;
      group = "dev";
      shell = pkgs.bashInteractive;
    };

    # The workspace mount, with a wp-content that already holds one plugin, as
    # a project tracking its own plugin in git would.
    systemd.tmpfiles.rules = [
      "d /workspace 0755 dev dev - -"
      "d ${site} 0755 dev dev - -"
      "d ${site}/wp-content 0755 dev dev - -"
      "d ${site}/wp-content/plugins 0755 dev dev - -"
      "L+ ${site}/wp-content/plugins/it-plugin - - - - ${plugin}"
    ];

    wordpress.enable = true;
  };

  testScript = ''
    machine.wait_for_unit("wordpress-setup.service")
    machine.wait_for_unit("caddy.service")

    # The core was copied into the shared web root next to the existing
    # wp-content, owned by the guest user and with development defaults.
    machine.succeed("test -e ${site}/wp-includes/version.php")
    machine.succeed("test -L ${site}/wp-content/plugins/it-plugin")
    machine.succeed("test \"$(stat -c %U ${site}/wp-config.php)\" = dev")
    machine.succeed("su - dev -c 'wp config get WP_ENVIRONMENT_TYPE' | grep -x local")

    machine.succeed("su - dev -c 'wp-site check'")

    # Plugins are managed in WordPress; WP-CLI works from any directory.
    machine.succeed("su - dev -c 'cd / && wp plugin activate it-plugin'")
    machine.succeed("curl -fs -H 'Host: localhost:8081' http://127.0.0.1/ | grep vm-plugin")

    # Mail sent through PHP-FPM with WordPress's own sender reaches Mailpit.
    machine.succeed("curl -fs -H 'Host: localhost:8081' 'http://127.0.0.1/?vm-mail' | grep -x sent")
    machine.wait_until_succeeds("curl -fs http://127.0.0.1:8025/api/v1/messages | grep -q 'vm fpm mail'", timeout=30)

    # Caddy listens on loopback only, where nixant's port forward connects, and
    # hides dotfiles and the error log in the project directory.
    machine.succeed("test \"$(ss -Hltn 'sport = :80' | awk '{print $4}' | sort -u)\" = 127.0.0.1:80")
    machine.succeed("su - dev -c 'echo SECRET=1 > ${site}/.env && echo log > ${site}/wp-content/debug.log'")
    machine.succeed("test \"$(${get "/.env"})\" = 404")
    machine.succeed("test \"$(${get "/wp-content/debug.log"})\" = 404")

    # Rerunning setup does not rewrite an unchanged wp-config.php.
    before = machine.succeed("stat -c %y ${site}/wp-config.php")
    machine.succeed("systemctl restart wordpress-setup.service")
    assert machine.succeed("stat -c %y ${site}/wp-config.php") == before, "setup rewrote wp-config.php"

    # A wp-config.php from elsewhere gets the guest's database back, and other
    # files in the web root are left alone.
    machine.succeed("su - dev -c 'wp config set DB_HOST db.example.com && echo kept > ${site}/robots.txt'")
    machine.succeed("systemctl restart wordpress-setup.service")
    machine.succeed("su - dev -c 'wp config get DB_HOST' | grep -x localhost:/run/mysqld/mysqld.sock")
    machine.succeed("grep -x kept ${site}/robots.txt")
    machine.succeed("su - dev -c 'wp plugin is-active it-plugin'")
    machine.succeed("su - dev -c 'wp-site check'")
  '';
}

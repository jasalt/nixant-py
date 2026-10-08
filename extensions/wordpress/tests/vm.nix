# NixOS VM test: the services, setup unit and `wp-site check` without Incus.
# nixant contributes only its options module; the guest user is defined here
# because nixant's container module normally creates it.
{ pkgs, nixant, wordpress }:
let
  plugin = pkgs.writeTextDir "it-plugin.php" ''
    <?php
    /* Plugin Name: VM test plugin */
    add_action( 'wp_footer', function () { echo '<!-- vm-plugin -->'; } );
  '';
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

    # The workspace mount, with one plugin in it.
    systemd.tmpfiles.rules = [
      "d /workspace 0755 dev dev - -"
      "d /workspace/plugins 0755 dev dev - -"
      "L+ /workspace/plugins/it-plugin - - - - ${plugin}"
    ];

    wordpress = {
      enable = true;
      plugins.it-plugin.path = "plugins/it-plugin";
    };
  };

  testScript = ''
    machine.wait_for_unit("wordpress-setup.service")
    machine.wait_for_unit("caddy.service")

    machine.succeed("su - dev -c 'wp-site check'")

    # The linked plugin is served, and WP-CLI works from any directory.
    machine.succeed("curl -fs -H 'Host: localhost:8081' http://127.0.0.1/ | grep vm-plugin")
    machine.succeed("su - dev -c 'cd / && wp plugin is-active it-plugin'")

    # Caddy listens on loopback only, where nixant's port forward connects.
    machine.succeed("test \"$(ss -Hltn 'sport = :80' | awk '{print $4}' | sort -u)\" = 127.0.0.1:80")

    # Rerunning setup converges without changes.
    machine.succeed("systemctl restart wordpress-setup.service")
    machine.succeed("su - dev -c 'wp-site check'")

    # Copying the core again keeps the user's files in the web root and removes
    # a file the previous core shipped but this one does not.
    machine.succeed(
      "su - dev -c '"
      "cd /var/lib/wordpress && echo kept > robots.txt && touch wp-admin/obsolete.php"
      " && echo ./wp-admin/obsolete.php >> .core-files && LC_ALL=C sort -o .core-files .core-files"
      " && rm .core-version'"
    )
    machine.succeed("systemctl restart wordpress-setup.service")
    machine.succeed("test -e /var/lib/wordpress/robots.txt")
    machine.fail("test -e /var/lib/wordpress/wp-admin/obsolete.php")
    machine.succeed("su - dev -c 'wp-site check'")
  '';
}

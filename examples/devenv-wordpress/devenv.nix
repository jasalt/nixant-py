# The WordPress stack, evaluated by devenv inside the guest. Run devenv only
# there (`nixant exec -- devenv ...`): .devenv/ holds the guest's store paths
# and the MariaDB data.
{ pkgs, lib, config, ... }:
let
  ports = import ./ports.nix;
  root = "${config.devenv.root}/wordpress";
  url = "http://localhost:${toString ports.http}";
  db = { name = "wordpress"; user = "wordpress"; password = "wordpress"; port = 3306; };
  smtp = "127.0.0.1:1025";
  # pkgs.wp-cli replaces php.ini with its own; run its phar with the PHP below
  # instead, so WP-CLI sees the same settings as the site (sendmail_path).
  wp-cli = pkgs.writeShellScriptBin "wp" ''
    exec ${config.languages.php.package}/bin/php ${pkgs.wp-cli}/share/wp-cli/wp-cli.phar "$@"
  '';
  wp = lib.getExe wp-cli;
  mariadb = "${pkgs.mariadb}/bin/mariadb";
in {
  # Development only: fixed credentials, no TLS.
  packages = [ wp-cli ];

  languages.php = {
    enable = true;
    # Route wp_mail() to Mailpit.
    ini = ''
      memory_limit = 256M
      upload_max_filesize = 64M
      post_max_size = 64M
      sendmail_path = "${lib.getExe pkgs.mailpit} sendmail --smtp-addr ${smtp}"
    '';
    # php-fpm runs as the guest user, which owns the shared WordPress root, so
    # uploads and plugin installs are owned by you on the host.
    fpm.pools.web.settings = {
      "pm" = "dynamic";
      "pm.max_children" = 5;
      "pm.start_servers" = 2;
      "pm.min_spare_servers" = 1;
      "pm.max_spare_servers" = 5;
    };
  };

  services.mysql = {
    enable = true;
    package = pkgs.mariadb;
    settings.mysqld = { port = db.port; bind-address = "127.0.0.1"; };
    initialDatabases = [ { name = db.name; } ];
    ensureUsers = [ {
      inherit (db) name password;
      # WordPress connects over TCP, which matches the IP, not "localhost".
      host = "127.0.0.1";
      ensurePermissions."${db.name}.*" = "ALL PRIVILEGES";
    } ];
  };

  services.caddy = {
    enable = true;
    virtualHosts."http://:${toString ports.http}".extraConfig = ''
      bind 127.0.0.1
      root * ${root}
      php_fastcgi unix/${config.languages.php.fpm.pools.web.socket}
      file_server
    '';
  };

  services.mailpit = {
    enable = true;
    uiListenAddress = "127.0.0.1:${toString ports.mailpit}";
    smtpListenAddress = smtp;
  };

  # Creates only what is missing and never overwrites: after the first run the
  # WordPress root is yours, edited on the host and updated by WordPress itself.
  tasks."wordpress:setup" = {
    description = "Download WordPress, write wp-config.php and install, if missing";
    exec = ''
      set -eu
      if [ ! -e ${root}/wp-load.php ]; then
        ${wp} core download --path=${root}
      fi
      if [ ! -e ${root}/wp-config.php ]; then
        ${wp} config create --path=${root} --skip-check \
          --dbname=${db.name} --dbuser=${db.user} --dbpass=${db.password} \
          --dbhost=127.0.0.1:${toString db.port}
      fi
      # WordPress sends from wordpress@<host>, and PHPMailer rejects a host
      # without a dot such as localhost.
      mail_from=${root}/wp-content/mu-plugins/devenv-mail-from.php
      if [ ! -e "$mail_from" ]; then
        mkdir -p "$(dirname "$mail_from")"
        cat > "$mail_from" <<'PHP'
      <?php
      // Written by wordpress:setup in devenv.nix: a valid sender for Mailpit.
      add_filter('wp_mail_from', fn () => 'wordpress@example.test');
      PHP
      fi
      tries=0
      until ${mariadb} -h 127.0.0.1 -P ${toString db.port} -u ${db.user} -p${db.password} \
          -e 'SELECT 1' ${db.name} >/dev/null 2>&1; do
        tries=$((tries + 1))
        if [ "$tries" -ge 30 ]; then
          echo "database not reachable; start the services first: devenv up -d" >&2
          exit 1
        fi
        sleep 2
      done
      if ! ${wp} core is-installed --path=${root}; then
        ${wp} core install --path=${root} --url=${url} --title="devenv WordPress" \
          --admin_user=admin --admin_password=password \
          --admin_email=admin@example.test --skip-email
      fi
    '';
  };
}

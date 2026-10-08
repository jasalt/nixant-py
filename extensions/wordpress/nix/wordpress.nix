{ config, lib, options, pkgs, ... }:
let
  inherit (lib) mkOption types;
  cfg = config.wordpress;

  # The module reads a few nixant.* options but does not import nixant, so the
  # client flake must. Everything that reads config.nixant stays behind this.
  hasNixant = options ? nixant;
  nixantPorts = lib.optionals hasNixant config.nixant.ports;

  safePath = path:
    !(lib.hasPrefix "/" path)
    && path != ""
    && !(builtins.elem ".." (lib.splitString "/" path));

  # The first forwarded port that reaches guest port 80 is the site's URL.
  derivedUrl =
    let web = builtins.filter (port: port.guest == 80) nixantPorts;
    in if web == [] then null else "http://localhost:${toString (builtins.head web).host}";

  # wordpress.url as { host; port; }, or null when it is not http://host[:port].
  urlParts =
    let match = if cfg.url == null then null
      else builtins.match "http://([A-Za-z0-9.-]+)(:([1-9][0-9]*))?" cfg.url;
    in if match == null then null else {
      host = builtins.elemAt match 0;
      port = let port = builtins.elemAt match 2; in if port == null then 80 else lib.toInt port;
    };
  # Guest ports the site's URL port must not take over (see the loopback note
  # on the Caddy site below).
  reservedPorts = [ cfg.mailpit.uiPort cfg.mailpit.smtpPort 3306 ];

  # nixpkgs' wordpress package drops the bundled themes and plugins, which
  # leaves a fresh site without a theme. The upstream tarball has them.
  upstreamCore = pkgs.runCommand "wordpress-core-${pkgs.wordpress.version}" {
    inherit (pkgs.wordpress) version;
  } ''
    mkdir "$out"
    tar -xzf ${pkgs.wordpress.src} --strip-components=1 -C "$out"
  '';
in {
  options.wordpress = {
    enable = lib.mkEnableOption "an isolated WordPress development site";

    root = mkOption {
      type = types.str;
      default = "public";
      description = ''
        Web root, relative to the project root (the workspace mount). The
        whole WordPress installation lives here on the host: core,
        wp-config.php and wp-content. An existing installation is used as it
        is; an empty or missing directory gets a fresh core.
      '';
    };

    package = mkOption {
      type = types.package;
      default = upstreamCore;
      defaultText = lib.literalExpression "the unpacked pkgs.wordpress.src tarball";
      description = ''
        WordPress core that seeds the web root when it has no wp-includes yet.
        After that WordPress owns its files and updates them itself. The
        default is the upstream tarball of nixpkgs' WordPress, whose own
        package omits the bundled themes and plugins.
      '';
    };

    listenAddress = mkOption {
      type = types.nullOr types.str;
      default = "127.0.0.1";
      example = null;
      description = ''
        Guest address Caddy serves the site on. nixant's port forward connects
        to the guest's 127.0.0.1, so the default keeps the site off the Incus
        bridge, where other instances could reach it. `null` listens on every
        interface, for example to browse a VM guest by its address.
      '';
    };

    phpPackage = mkOption {
      type = types.package;
      default = pkgs.php;
      defaultText = lib.literalExpression "pkgs.php";
      description = ''
        PHP used by PHP-FPM and WP-CLI. It must provide mysqli, pdo_mysql, gd,
        zip, exif and intl; nixpkgs' default build does.
      '';
    };

    title = mkOption {
      type = types.str;
      default = "WordPress";
      description = "Site title, used at the first install only.";
    };

    url = mkOption {
      type = types.nullOr types.str;
      default = derivedUrl;
      defaultText = lib.literalExpression ''"http://localhost:<host port forwarded to guest 80>"'';
      example = "http://localhost:8081";
      description = ''
        Site URL, `http://<host>[:<port>]` without a path, kept in the home
        and siteurl options. Derived from the `nixant.ports` entry whose guest
        port is 80; set it explicitly when there is none. The guest serves the
        site on this URL's port too and resolves its host to loopback, so
        WordPress can request its own URL (WP-Cron, Site Health, static
        exporters).
      '';
    };

    admin = {
      user = mkOption { type = types.str; default = "admin"; description = "Development-only administrator login, used at the first install only."; };
      password = mkOption { type = types.str; default = "password"; description = "Development-only administrator password, used at the first install only."; };
      email = mkOption { type = types.str; default = "admin@example.test"; description = "Administrator email address, used at the first install only."; };
    };

    mailFrom = mkOption {
      type = types.str;
      default = "wordpress@example.test";
      description = ''
        Sender address of WordPress mail whose own sender is not a valid
        address. WordPress derives `wordpress@<host>` by default, and
        `wordpress@localhost` is rejected, so mail would never reach Mailpit.
      '';
    };

    mailpit = {
      uiPort = mkOption { type = types.port; default = 8025; description = "Guest port of the Mailpit web UI."; };
      smtpPort = mkOption { type = types.port; default = 1025; description = "Guest port of the Mailpit SMTP server."; };
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      assertions = [
        {
          assertion = hasNixant;
          message = "wordpress: the nixant options are missing; import nixant.nixosModules.container (or nixant.nixosModules.vm) next to the wordpress module.";
        }
        {
          assertion = safePath cfg.root;
          message = "wordpress.root must be relative to the project root and must not contain '..'; got '${cfg.root}'.";
        }
        {
          assertion = cfg.url != null;
          message = "wordpress.url is not set and cannot be derived: forward a host port to guest port 80 with nixant.ports, or set wordpress.url = \"http://localhost:8081\".";
        }
        {
          assertion = cfg.url == null || lib.hasPrefix "http://" cfg.url;
          message = "wordpress.url must start with http:// (TLS is not supported); got '${toString cfg.url}'.";
        }
        {
          assertion = cfg.url == null || !(lib.hasPrefix "http://" cfg.url)
            || (urlParts != null && urlParts.port <= 65535);
          message = "wordpress.url must be http://<host>[:<port>], without a path or trailing slash (the site is served from /); got '${toString cfg.url}'.";
        }
        {
          assertion = urlParts == null || !(builtins.elem urlParts.port reservedPorts);
          message = "wordpress.url uses port ${toString (urlParts.port or "")}, which the guest also serves the site on, but Mailpit or MariaDB already uses it in the guest; forward another host port to guest port 80.";
        }
      ];
    }
    # Everything below reads config.nixant, so it stays behind hasNixant: a
    # missing nixant module then fails on the assertion above, not on a lookup.
    (lib.mkIf hasNixant (
      let
        user = config.nixant.user.name;
        group = config.users.users.${user}.group;
        root = "${config.nixant.mounts.workspace.target}/${cfg.root}";
        pool = config.services.phpfpm.pools.wordpress;
        db = { name = "wordpress"; socket = "/run/mysqld/mysqld.sock"; };
        sendmailPath = "${pkgs.mailpit}/bin/mailpit sendmail -S 127.0.0.1:${toString cfg.mailpit.smtpPort}";
        # Runs before every PHP script in the guest, so the sender fix needs no
        # file in the project's wp-content (which may be deployed elsewhere).
        # WordPress turns callbacks registered in $wp_filter before it loads
        # into hooks.
        mailPrepend = pkgs.writeText "nixant-wp-prepend.php" ''
          <?php
          // nixant-wp: replace an invalid WordPress mail sender, such as
          // wordpress@localhost, so mail reaches Mailpit.
          $GLOBALS['wp_filter']['wp_mail_from'][10][] = array(
            'function' => static function ( $from ) {
              return is_email( $from ) ? $from : ${builtins.toJSON cfg.mailFrom};
            },
            'accepted_args' => 1,
          );
        '';
        # wp-cli runs on the module's PHP with its own ini, which needs the same
        # mail settings as PHP-FPM so wp_mail from the CLI reaches Mailpit.
        wpCli = pkgs.wp-cli.override {
          php = cfg.phpPackage;
          phpIniFile = pkgs.writeText "php.ini" ''
            memory_limit = -1
            phar.readonly = Off
            sendmail_path = ${sendmailPath}
            auto_prepend_file = ${mailPrepend}
          '';
        };
        wpSite = pkgs.callPackage ./wp-site.nix { wp-cli = wpCli; };
        uiPort = builtins.filter (port: port.guest == cfg.mailpit.uiPort) config.nixant.ports;
        # Everything wp-site setup needs; a change reruns the setup unit.
        settingsFile = pkgs.writeText "wordpress-site.json" (builtins.toJSON {
          inherit root;
          inherit (cfg) title url admin;
          mailpit = { inherit (cfg.mailpit) uiPort smtpPort; };
          # `wp-site check` requests the site here.
          httpAddress = if cfg.listenAddress == null then "127.0.0.1" else cfg.listenAddress;
          core = "${cfg.package}";
          db = db // { user = user; };
          mailpitUrl = if uiPort == [] then null
            else "http://localhost:${toString (builtins.head uiPort).host}";
        });
      in {
        assertions = [{
          assertion = config.nixant.mounts.workspace.enable;
          message = "wordpress.root lives in the workspace mount; keep nixant.mounts.workspace enabled.";
        }];

        # PHP-FPM, Caddy and the setup unit all run as the nixant user, so the
        # idmapped workspace mount is readable and writable without extra groups,
        # and files WordPress writes are owned by the host user.
        # `nixant exec -- wp ...` works from any directory: bash -lc reads
        # /etc/profile, which exports WP_CLI_CONFIG_PATH.
        environment.systemPackages = [ wpCli wpSite ];
        environment.etc."wp-cli/config.yml".text = "path: ${root}\n";
        # `wp-site check` finds the settings here when run by hand.
        environment.etc."wordpress/site.json".source = settingsFile;
        environment.variables.WP_CLI_CONFIG_PATH = "/etc/wp-cli/config.yml";

        # The URL's host must reach this guest from inside it, too. localhost
        # and IP addresses need no entry.
        networking.hosts = lib.mkIf (urlParts != null
          && urlParts.host != "localhost"
          && builtins.match "[0-9.]+" urlParts.host == null) {
          "127.0.0.1" = [ urlParts.host ];
        };

        # unix_socket authentication: the database user is the nixant user and
        # has no password.
        services.mysql = {
          enable = true;
          package = pkgs.mariadb;
          ensureDatabases = [ db.name ];
          ensureUsers = [{
            name = user;
            ensurePermissions."${db.name}.*" = "ALL PRIVILEGES";
          }];
        };

        services.phpfpm.pools.wordpress = {
          inherit user group;
          phpPackage = cfg.phpPackage;
          phpOptions = ''
            upload_max_filesize = 64M
            post_max_size = 64M
            memory_limit = 512M
            sendmail_path = ${sendmailPath}
            auto_prepend_file = ${mailPrepend}
            ; Edits on the host must show up on the next request.
            opcache.validate_timestamps = 1
            opcache.revalidate_freq = 0
          '';
          settings = {
            "listen.owner" = user;
            "listen.group" = group;
            "listen.mode" = "0660";
            "pm" = "dynamic";
            "pm.max_children" = 8;
            "pm.start_servers" = 2;
            "pm.min_spare_servers" = 1;
            "pm.max_spare_servers" = 3;
            "catch_workers_output" = true;
          };
        };

        # Reload goes through the admin API, which is off.
        services.caddy = {
          enable = true;
          inherit user group;
          enableReload = false;
          globalConfig = "admin off";
          # nixant forwards the host port to guest port 80, but WordPress
          # requests its own URL (WP-Cron, Site Health, static exporters) from
          # inside the guest, at the URL's port. Serve that port as well; the
          # bind below keeps it on loopback too.
          virtualHosts.":80".serverAliases =
            lib.optional (urlParts != null && urlParts.port != 80) ":${toString urlParts.port}";
          virtualHosts.":80".extraConfig = lib.optionalString (cfg.listenAddress != null) ''
            bind ${cfg.listenAddress}
          '' + ''
            root * ${root}
            # The web root is a project directory: hide dotfiles anywhere in it
            # (.git, .env, .user.ini) and the PHP error log.
            @hidden path_regexp /\.|^/wp-content/debug\.log$
            respond @hidden 404
            encode gzip
            php_fastcgi unix/${pool.socket}
            file_server
          '';
        };

        # Seeds the core and installs the site when needed, then keeps the
        # database settings and the URL current; a failure shows up as a
        # degraded activation (journalctl -u wordpress-setup).
        systemd.services.wordpress-setup = {
          description = "Set up the WordPress site";
          wantedBy = [ "multi-user.target" ];
          after = [ "mysql.service" ];
          requires = [ "mysql.service" ];
          unitConfig.RequiresMountsFor = root;
          restartTriggers = [ settingsFile ];
          environment = {
            WP_SITE_SETTINGS = settingsFile;
            # Keep WP-CLI's cache in the guest, out of the project.
            WP_CLI_CACHE_DIR = "/var/cache/wordpress-setup";
          };
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            User = user;
            Group = group;
            CacheDirectory = "wordpress-setup";
            ExecStart = "${wpSite}/bin/wp-site setup";
            TimeoutStartSec = 600;
          };
        };

        services.mailpit.instances.wordpress = {
          listen = "127.0.0.1:${toString cfg.mailpit.uiPort}";
          smtp = "127.0.0.1:${toString cfg.mailpit.smtpPort}";
        };
      }
    ))
  ]);
}

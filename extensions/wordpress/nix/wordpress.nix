{ config, lib, options, pkgs, ... }:
let
  inherit (lib) mkOption types;
  cfg = config.wordpress;

  # The module reads a few nixant.* options but does not import nixant, so the
  # client flake must. Everything that reads config.nixant stays behind this.
  hasNixant = options ? nixant;
  nixantPorts = lib.optionals hasNixant config.nixant.ports;

  slug = "[A-Za-z0-9_-]+";
  isSlug = value: builtins.match slug value != null;
  safePath = path:
    !(lib.hasPrefix "/" path)
    && path != ""
    && !(builtins.elem ".." (lib.splitString "/" path));

  # The first forwarded port that reaches guest port 80 is the site's URL.
  derivedUrl =
    let web = builtins.filter (port: port.guest == 80) nixantPorts;
    in if web == [] then null else "http://localhost:${toString (builtins.head web).host}";

  componentOptions = description: {
    path = mkOption {
      type = types.str;
      example = "plugins/my-plugin";
      description = "${description} directory, relative to the workspace mount.";
    };
  };
  pluginType = types.submodule {
    options = componentOptions "Plugin" // {
      activate = mkOption {
        type = types.bool;
        default = true;
        description = "Activate the plugin during setup.";
      };
    };
  };
  themeType = types.submodule { options = componentOptions "Theme"; };

  # nixpkgs' wordpress package drops the bundled themes and plugins, which
  # leaves a fresh site without a theme. The upstream tarball has them.
  upstreamCore = pkgs.runCommand "wordpress-core-${pkgs.wordpress.version}" {
    inherit (pkgs.wordpress) version;
  } ''
    mkdir "$out"
    tar -xzf ${pkgs.wordpress.src} --strip-components=1 -C "$out"
  '';

  bad = names: lib.concatStringsSep ", " (map (name: "'${name}'") names);
  unsafe = components:
    builtins.attrNames (lib.filterAttrs (_: component: !safePath component.path) components);
in {
  options.wordpress = {
    enable = lib.mkEnableOption "an isolated WordPress development site";

    package = mkOption {
      type = types.package;
      default = upstreamCore;
      defaultText = lib.literalExpression "the unpacked pkgs.wordpress.src tarball";
      description = ''
        WordPress core source, copied into /var/lib/wordpress by the setup
        unit. The default is the upstream tarball of nixpkgs' WordPress, whose
        own package omits the bundled themes and plugins.
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
        Site URL. Derived from the `nixant.ports` entry whose guest port is 80;
        set it explicitly when there is none.
      '';
    };

    admin = {
      user = mkOption { type = types.str; default = "admin"; description = "Development-only administrator login."; };
      password = mkOption { type = types.str; default = "password"; description = "Development-only administrator password."; };
      email = mkOption { type = types.str; default = "admin@example.test"; description = "Administrator email address."; };
    };

    plugins = mkOption {
      type = types.attrsOf pluginType;
      default = {};
      description = "Plugins linked from the workspace into wp-content/plugins, by slug.";
    };

    themes = mkOption {
      type = types.attrsOf themeType;
      default = {};
      description = "Themes linked from the workspace into wp-content/themes, by slug.";
    };

    activeTheme = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "Slug of the theme to activate; installed from wordpress.org if not linked or bundled.";
    };

    wpConfig = mkOption {
      type = types.attrsOf (types.oneOf [ types.bool types.int types.str ]);
      default = {};
      description = ''
        Constants applied to wp-config.php with `wp config set` on every
        setup. Debugging is on by default (`WP_DEBUG` and `WP_DEBUG_LOG`
        true, `WP_DEBUG_DISPLAY` false); these defaults are set per key, so
        adding constants keeps them and any key can be overridden.
      '';
    };

    mailpit = {
      uiPort = mkOption { type = types.port; default = 8025; description = "Guest port of the Mailpit web UI."; };
      smtpPort = mkOption { type = types.port; default = 1025; description = "Guest port of the Mailpit SMTP server."; };
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      wordpress.wpConfig = {
        WP_DEBUG = lib.mkDefault true;
        WP_DEBUG_LOG = lib.mkDefault true;
        WP_DEBUG_DISPLAY = lib.mkDefault false;
      };

      assertions = [
        {
          assertion = hasNixant;
          message = "wordpress: the nixant options are missing; import nixant.nixosModules.container (or nixant.nixosModules.vm) next to the wordpress module.";
        }
        {
          assertion = !(builtins.any (name: !isSlug name) (builtins.attrNames cfg.plugins));
          message = "wordpress.plugins: slugs must match ${slug}; got ${bad (builtins.filter (name: !isSlug name) (builtins.attrNames cfg.plugins))}.";
        }
        {
          assertion = !(builtins.any (name: !isSlug name) (builtins.attrNames cfg.themes));
          message = "wordpress.themes: slugs must match ${slug}; got ${bad (builtins.filter (name: !isSlug name) (builtins.attrNames cfg.themes))}.";
        }
        {
          assertion = unsafe cfg.plugins == [];
          message = "wordpress.plugins: paths must be relative to the workspace and must not contain '..'; check ${bad (unsafe cfg.plugins)}.";
        }
        {
          assertion = unsafe cfg.themes == [];
          message = "wordpress.themes: paths must be relative to the workspace and must not contain '..'; check ${bad (unsafe cfg.themes)}.";
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
          assertion = !(builtins.any (name: builtins.match "[A-Za-z_][A-Za-z0-9_]*" name == null) (builtins.attrNames cfg.wpConfig));
          message = "wordpress.wpConfig: names must be PHP constant names; got ${bad (builtins.filter (name: builtins.match "[A-Za-z_][A-Za-z0-9_]*" name == null) (builtins.attrNames cfg.wpConfig))}.";
        }
        {
          assertion = cfg.activeTheme == null || isSlug cfg.activeTheme;
          message = "wordpress.activeTheme must match ${slug}; got '${toString cfg.activeTheme}'.";
        }
      ];
    }
    # Everything below reads config.nixant, so it stays behind hasNixant: a
    # missing nixant module then fails on the assertion above, not on a lookup.
    (lib.mkIf hasNixant (
      let
        user = config.nixant.user.name;
        group = config.users.users.${user}.group;
        root = "/var/lib/wordpress";
        pool = config.services.phpfpm.pools.wordpress;
        db = { name = "wordpress"; socket = "/run/mysqld/mysqld.sock"; };
        sendmailPath = "${pkgs.mailpit}/bin/mailpit sendmail -S 127.0.0.1:${toString cfg.mailpit.smtpPort}";
        # wp-cli runs on the module's PHP with its own ini, which needs the same
        # sendmail_path as PHP-FPM so wp_mail from the CLI reaches Mailpit.
        wpCli = pkgs.wp-cli.override {
          php = cfg.phpPackage;
          phpIniFile = pkgs.writeText "php.ini" ''
            memory_limit = -1
            phar.readonly = Off
            sendmail_path = ${sendmailPath}
          '';
        };
        wpSite = pkgs.callPackage ./wp-site.nix { wp-cli = wpCli; };
        uiPort = builtins.filter (port: port.guest == cfg.mailpit.uiPort) config.nixant.ports;
        # Everything wp-site setup needs; a change reruns the setup unit.
        settingsFile = pkgs.writeText "wordpress-site.json" (builtins.toJSON {
          inherit root;
          inherit (cfg) title url admin wpConfig plugins themes activeTheme;
          workspace = config.nixant.mounts.workspace.target;
          core = "${cfg.package}";
          coreId = builtins.baseNameOf "${cfg.package}";
          db = db // { user = user; };
          mailpitUrl = if uiPort == [] then null
            else "http://localhost:${toString (builtins.head uiPort).host}";
        });
      in {
        assertions = [{
          assertion = (cfg.plugins == {} && cfg.themes == {}) || config.nixant.mounts.workspace.enable;
          message = "wordpress.plugins and wordpress.themes link from the workspace mount; keep nixant.mounts.workspace enabled.";
        }];

        # PHP-FPM, Caddy and the setup unit all run as the nixant user, so the
        # idmapped workspace mount is readable and writable without extra groups.
        # `nixant exec -- wp ...` works from any directory: bash -lc reads
        # /etc/profile, which exports WP_CLI_CONFIG_PATH.
        environment.systemPackages = [ wpCli wpSite ];
        environment.etc."wp-cli/config.yml".text = "path: ${root}\n";
        environment.variables.WP_CLI_CONFIG_PATH = "/etc/wp-cli/config.yml";

        systemd.tmpfiles.rules = [ "d ${root} 0750 ${user} ${group} - -" ];

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
          virtualHosts.":80".extraConfig = ''
            root * ${root}
            # Setup state (.core-version, .cache) lives in the web root.
            @dotfiles path /.*
            respond @dotfiles 404
            encode gzip
            php_fastcgi unix/${pool.socket}
            file_server
          '';
        };

        # Converges core, wp-config and the install; a failure shows up as a
        # degraded activation (journalctl -u wordpress-setup).
        systemd.services.wordpress-setup = {
          description = "Converge the WordPress site";
          wantedBy = [ "multi-user.target" ];
          after = [ "mysql.service" ];
          requires = [ "mysql.service" ];
          restartTriggers = [ settingsFile ];
          environment.WP_SITE_SETTINGS = settingsFile;
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            User = user;
            Group = group;
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

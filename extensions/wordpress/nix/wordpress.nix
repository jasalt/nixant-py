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

  bad = names: lib.concatStringsSep ", " (map (name: "'${name}'") names);
  unsafe = components:
    builtins.attrNames (lib.filterAttrs (_: component: !safePath component.path) components);
in {
  options.wordpress = {
    enable = lib.mkEnableOption "an isolated WordPress development site";

    package = mkOption {
      type = types.package;
      default = pkgs.wordpress;
      defaultText = lib.literalExpression "pkgs.wordpress";
      description = ''
        WordPress core source. nixpkgs' package omits the bundled themes and
        plugins.
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
      default = {
        WP_DEBUG = true;
        WP_DEBUG_LOG = true;
        WP_DEBUG_DISPLAY = false;
      };
      description = "Constants applied to wp-config.php with `wp config set` on every setup.";
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
          assertion = cfg.activeTheme == null || isSlug cfg.activeTheme;
          message = "wordpress.activeTheme must match ${slug}; got '${toString cfg.activeTheme}'.";
        }
      ];
    }
    # Services and setup (reading config.nixant) go here behind hasNixant, so
    # a missing nixant module fails on the assertion above, not on a lookup.
    (lib.mkIf hasNixant { })
  ]);
}

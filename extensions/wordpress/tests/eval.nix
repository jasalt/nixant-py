{ nixpkgs, system, nixant, wordpress }:
let
  lib = nixpkgs.lib;
  # A guest configuration the way a client flake builds it: nixant's container
  # module, this module, then the client's settings.
  evaluate = { withNixant ? true, extra ? {} }: lib.nixosSystem {
    inherit system;
    modules =
      lib.optional withNixant nixant.nixosModules.container
      ++ [
        wordpress
        ({ lib, ... }: {
          boot.isContainer = true;
          system.stateVersion = "25.05";
          wordpress.enable = true;
        } // lib.optionalAttrs withNixant {
          nixant = {
            instanceName = "test-dev";
            ports = [ { host = 8081; guest = 80; } ];
          };
        })
        extra
      ];
  };

  failures = args:
    map (item: item.message)
      (builtins.filter (item: !item.assertion) (evaluate args).config.assertions);
  succeeds = value: (builtins.tryEval (builtins.deepSeq value true)).success;
  valid = args: failures args == [] && succeeds (evaluate args).config.system.build.toplevel.drvPath;
  # True when exactly the expected assertion fires.
  fails = needle: args:
    let messages = failures args;
    in messages != [] && lib.all (message: lib.hasInfix needle message) messages;

  base_ = evaluate {};
  settingsOf = system:
    builtins.fromJSON (builtins.unsafeDiscardStringContext
      (builtins.readFile system.config.systemd.services.wordpress-setup.environment.WP_SITE_SETTINGS));
  base = base_.config.wordpress;
  tests = {
    validConfigBuilds = valid {};
    disabledNeedsNothing = succeeds (evaluate {
      withNixant = false;
      extra.wordpress.enable = lib.mkForce false;
    }).config.system.build.toplevel.drvPath;
    urlDerivedFromPort = base.url == "http://localhost:8081";
    urlExplicitWins = (evaluate { extra.wordpress.url = "http://localhost:9000"; }).config.wordpress.url == "http://localhost:9000";
    servicesExist = let c = base_.config; in
      c.services.mysql.enable && c.services.caddy.enable
      && c.services.phpfpm.pools ? wordpress && c.services.mailpit.instances ? wordpress;
    poolRunsAsNixantUser = let c = base_.config; in
      c.services.phpfpm.pools.wordpress.user == "dev" && c.services.caddy.user == "dev";
    databaseUserIsNixantUser = (builtins.head base_.config.services.mysql.ensureUsers).name == "dev";
    caddyServesWordpress = let site = base_.config.services.caddy.virtualHosts.":80".extraConfig; in
      lib.hasInfix "root * /var/lib/wordpress" site && lib.hasInfix "/run/phpfpm/wordpress.sock" site;
    caddyAdminOff = lib.hasInfix "admin off" base_.config.services.caddy.globalConfig
      && !base_.config.services.caddy.enableReload;
    mailpitPorts = let m = base_.config.services.mailpit.instances.wordpress; in
      m.listen == "127.0.0.1:8025" && m.smtp == "127.0.0.1:1025";
    mailpitPortsConfigurable = let m = (evaluate { extra.wordpress.mailpit = { uiPort = 9025; smtpPort = 9026; }; }).config.services.mailpit.instances.wordpress; in
      m.listen == "127.0.0.1:9025" && m.smtp == "127.0.0.1:9026";
    stateDirectoryRule = lib.elem "d /var/lib/wordpress 0750 dev dev - -" base_.config.systemd.tmpfiles.rules;
    setupUnit = let u = base_.config.systemd.services.wordpress-setup; in
      u.serviceConfig.Type == "oneshot" && u.serviceConfig.RemainAfterExit
      && u.serviceConfig.User == "dev"
      && lib.elem "mysql.service" u.after && lib.elem "mysql.service" u.requires
      && lib.elem "multi-user.target" u.wantedBy
      && lib.hasSuffix "/bin/wp-site setup" u.serviceConfig.ExecStart;
    setupSettings = let
      u = base_.config.systemd.services.wordpress-setup;
      settings = settingsOf base_;
    in u.restartTriggers == [ u.environment.WP_SITE_SETTINGS ]
      && settings.url == "http://localhost:8081" && settings.db.user == "dev"
      && settings.mailpitUrl == null
      && lib.hasPrefix "/nix/store/" settings.core && lib.hasInfix settings.coreId settings.core;
    mailpitUrlFromPorts = (settingsOf (evaluate {
      extra.nixant.ports = lib.mkForce [ { host = 8081; guest = 80; } { host = 9025; guest = 8025; } ];
    })).mailpitUrl == "http://localhost:9025";
    titleChangeRerunsSetup = let
      path = args: (evaluate args).config.systemd.services.wordpress-setup.environment.WP_SITE_SETTINGS;
    in path {} != path { extra.wordpress.title = "Other"; };
    coreIncludesThemes = builtins.pathExists "${base.package}/wp-content/themes/twentytwentyfive";
    dotfilesHidden = lib.hasInfix "respond @dotfiles 404" base_.config.services.caddy.virtualHosts.":80".extraConfig;
    fpmSendsMailToMailpit = lib.hasInfix "mailpit sendmail -S 127.0.0.1:1025"
      base_.config.services.phpfpm.pools.wordpress.phpOptions;
    opcacheRevalidates = lib.hasInfix "opcache.revalidate_freq = 0"
      base_.config.services.phpfpm.pools.wordpress.phpOptions;
    mailPortFollowsOption = lib.hasInfix "-S 127.0.0.1:9026"
      (evaluate { extra.wordpress.mailpit.smtpPort = 9026; }).config.services.phpfpm.pools.wordpress.phpOptions;
    wpConfigInSettings = (settingsOf base_).wpConfig.WP_DEBUG == true
      && (settingsOf (evaluate { extra.wordpress.wpConfig.WP_HOME_NAME = "x"; })).wpConfig.WP_HOME_NAME == "x";
    wpCliConfigured = base_.config.environment.variables.WP_CLI_CONFIG_PATH == "/etc/wp-cli/config.yml"
      && base_.config.environment.etc."wp-cli/config.yml".text == "path: /var/lib/wordpress\n"
      && lib.any (p: (p.pname or "") == "wp-cli") base_.config.environment.systemPackages;
    wpConfigDefaultsMerge = let c = (evaluate { extra.wordpress.wpConfig = { WP_POST_REVISIONS = 3; WP_DEBUG = false; }; }).config.wordpress.wpConfig;
      in c.WP_POST_REVISIONS == 3 && c.WP_DEBUG == false && c.WP_DEBUG_LOG == true && c.WP_DEBUG_DISPLAY == false;
    mailFromPlugin = let
      settings = settingsOf (evaluate { extra.wordpress.mailFrom = "dev@client.test"; });
      plugin = builtins.readFile (builtins.unsafeDiscardStringContext settings.muPlugin);
    in lib.hasInfix "wp_mail_from" plugin && lib.hasInfix ''"dev@client.test"'' plugin;
    checkSettingsExported = base_.config.environment.etc."wordpress/site.json".source
        == base_.config.systemd.services.wordpress-setup.environment.WP_SITE_SETTINGS
      && (settingsOf base_).mailpit.uiPort == 8025;
    agentIsolationKeepsUrl = valid { extra.nixant.isolation = "agent"; }
      && (evaluate { extra.nixant.isolation = "agent"; }).config.wordpress.url == "http://localhost:8081";
    badConstantName = fails "PHP constant names" { extra.wordpress.wpConfig."BAD NAME" = true; };
    componentsInSettings = let
      settings = settingsOf (evaluate { extra.wordpress = {
        plugins.my-plugin = { path = "plugins/my-plugin"; };
        plugins.inactive = { path = "plugins/inactive"; activate = false; };
        themes.my-theme.path = "themes/my-theme";
        activeTheme = "my-theme";
      }; });
    in settings.workspace == "/workspace"
      && settings.plugins.my-plugin == { path = "plugins/my-plugin"; activate = true; }
      && !settings.plugins.inactive.activate
      && settings.themes.my-theme.path == "themes/my-theme" && settings.activeTheme == "my-theme";
    componentsNeedWorkspace = fails "keep nixant.mounts.workspace enabled" {
      extra.wordpress.plugins.p.path = "p";
      extra.nixant.mounts.workspace.enable = false;
    };
    nothingWhenDisabled = !(evaluate { extra.wordpress.enable = lib.mkForce false; }).config.services.caddy.enable;
    defaultsAreDevelopmentOnly = base.admin.user == "admin" && base.wpConfig.WP_DEBUG_DISPLAY == false;

    missingNixant = fails "nixant options are missing" { withNixant = false; extra.wordpress.url = "http://localhost:8081"; };
    badPluginSlug = fails "wordpress.plugins: slugs" { extra.wordpress.plugins."bad slug".path = "plugins/x"; };
    badThemeSlug = fails "wordpress.themes: slugs" { extra.wordpress.themes."a/b".path = "themes/x"; };
    parentPath = fails "must not contain '..'" { extra.wordpress.plugins.p.path = "../outside"; };
    nestedParentPath = fails "must not contain '..'" { extra.wordpress.themes.t.path = "a/../../b"; };
    absolutePath = fails "relative to the workspace" { extra.wordpress.plugins.p.path = "/etc"; };
    emptyPath = fails "relative to the workspace" { extra.wordpress.plugins.p.path = ""; };
    goodPaths = valid { extra.wordpress = { plugins.my-plugin.path = "plugins/my-plugin"; themes.my_theme.path = "themes/my_theme"; }; };
    dotsInNamesAllowed = valid { extra.wordpress.plugins.p.path = "plugins/v1..2/p"; };
    underivableUrl = fails "cannot be derived" { extra.nixant.ports = lib.mkForce []; };
    nonHttpUrl = fails "must start with http://" { extra.wordpress.url = "https://localhost"; };
    badActiveTheme = fails "activeTheme" { extra.wordpress.activeTheme = "no spaces"; };
    goodActiveTheme = valid { extra.wordpress.activeTheme = "twentytwentyfive"; };
  };
in assert lib.assertMsg (lib.all (value: value) (builtins.attrValues tests))
  "nixant-wp eval tests failed: ${lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) tests))}";
  tests

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
      lib.hasInfix "root * /workspace/public" site && lib.hasInfix "/run/phpfpm/wordpress.sock" site;
    caddyAdminOff = lib.hasInfix "admin off" base_.config.services.caddy.globalConfig
      && !base_.config.services.caddy.enableReload;
    mailpitPorts = let m = base_.config.services.mailpit.instances.wordpress; in
      m.listen == "127.0.0.1:8025" && m.smtp == "127.0.0.1:1025";
    mailpitPortsConfigurable = let m = (evaluate { extra.wordpress.mailpit = { uiPort = 9025; smtpPort = 9026; }; }).config.services.mailpit.instances.wordpress; in
      m.listen == "127.0.0.1:9025" && m.smtp == "127.0.0.1:9026";
    setupUnit = let u = base_.config.systemd.services.wordpress-setup; in
      u.serviceConfig.Type == "oneshot" && u.serviceConfig.RemainAfterExit
      && u.serviceConfig.User == "dev"
      && lib.elem "mysql.service" u.after && lib.elem "mysql.service" u.requires
      && lib.elem "multi-user.target" u.wantedBy
      && u.unitConfig.RequiresMountsFor == "/workspace/public"
      && u.serviceConfig.CacheDirectory == "wordpress-setup"
      && u.environment.WP_CLI_CACHE_DIR == "/var/cache/wordpress-setup"
      && lib.hasSuffix "/bin/wp-site setup" u.serviceConfig.ExecStart;
    setupSettings = let
      u = base_.config.systemd.services.wordpress-setup;
      settings = settingsOf base_;
    in u.restartTriggers == [ u.environment.WP_SITE_SETTINGS ]
      && settings.root == "/workspace/public"
      && settings.url == "http://localhost:8081" && settings.db.user == "dev"
      && settings.mailpitUrl == null
      && lib.hasPrefix "/nix/store/" settings.core;
    rootIsConfigurable = let system = evaluate { extra.wordpress.root = "site/web"; }; in
      (settingsOf system).root == "/workspace/site/web"
      && lib.hasInfix "root * /workspace/site/web" system.config.services.caddy.virtualHosts.":80".extraConfig
      && system.config.environment.etc."wp-cli/config.yml".text == "path: /workspace/site/web\n";
    rootFollowsWorkspaceTarget = (settingsOf (evaluate {
      extra.nixant.mounts.workspace.target = "/srv/project";
    })).root == "/srv/project/public";
    mailpitUrlFromPorts = (settingsOf (evaluate {
      extra.nixant.ports = lib.mkForce [ { host = 8081; guest = 80; } { host = 9025; guest = 8025; } ];
    })).mailpitUrl == "http://localhost:9025";
    titleChangeRerunsSetup = let
      path = args: (evaluate args).config.systemd.services.wordpress-setup.environment.WP_SITE_SETTINGS;
    in path {} != path { extra.wordpress.title = "Other"; };
    coreIncludesThemes = builtins.pathExists "${base.package}/wp-content/themes/twentytwentyfive";
    hiddenPaths = lib.hasInfix ''@hidden path_regexp /\.|^/wp-content/debug\.log$''
      base_.config.services.caddy.virtualHosts.":80".extraConfig;
    caddyBindsLoopback = lib.hasInfix "bind 127.0.0.1\n" base_.config.services.caddy.virtualHosts.":80".extraConfig
      && (settingsOf base_).httpAddress == "127.0.0.1";
    # WordPress requests its own URL from inside the guest: serve its port.
    caddyServesUrlPort = base_.config.services.caddy.virtualHosts.":80".serverAliases == [ ":8081" ];
    noExtraPortOn80 = (evaluate { extra.wordpress.url = "http://localhost"; })
      .config.services.caddy.virtualHosts.":80".serverAliases == [];
    explicitUrlPortServed = (evaluate { extra.wordpress.url = "http://localhost:9000"; })
      .config.services.caddy.virtualHosts.":80".serverAliases == [ ":9000" ];
    customHostResolvesToGuest = let system = evaluate { extra.wordpress.url = "http://client.test:8081"; }; in
      lib.elem "client.test" system.config.networking.hosts."127.0.0.1"
      && system.config.services.caddy.virtualHosts.":80".serverAliases == [ ":8081" ];
    localhostNeedsNoHostsEntry = base_.config.networking.hosts
      == (evaluate { extra.wordpress.enable = lib.mkForce false; }).config.networking.hosts;
    caddyListensEverywhere = let system = evaluate { extra.wordpress.listenAddress = null; }; in
      !(lib.hasInfix "bind " system.config.services.caddy.virtualHosts.":80".extraConfig)
      && (settingsOf system).httpAddress == "127.0.0.1";
    fpmSendsMailToMailpit = lib.hasInfix "mailpit sendmail -S 127.0.0.1:1025"
      base_.config.services.phpfpm.pools.wordpress.phpOptions;
    opcacheRevalidates = lib.hasInfix "opcache.revalidate_freq = 0"
      base_.config.services.phpfpm.pools.wordpress.phpOptions;
    mailPortFollowsOption = lib.hasInfix "-S 127.0.0.1:9026"
      (evaluate { extra.wordpress.mailpit.smtpPort = 9026; }).config.services.phpfpm.pools.wordpress.phpOptions;
    wpCliConfigured = base_.config.environment.variables.WP_CLI_CONFIG_PATH == "/etc/wp-cli/config.yml"
      && base_.config.environment.etc."wp-cli/config.yml".text == "path: /workspace/public\n"
      && lib.any (p: (p.pname or "") == "wp-cli") base_.config.environment.systemPackages;
    # FPM and WP-CLI both prepend the same guest-only file that fixes the sender.
    mailFromPrepend = let
      system = evaluate { extra.wordpress.mailFrom = "dev@client.test"; };
      cli = lib.findFirst (p: (p.pname or "") == "wp-cli") null system.config.environment.systemPackages;
      # Reading the WP-CLI ini builds it, which makes the file it names readable.
      cliIni = builtins.readFile "${cli}/etc/php.ini";
      path = builtins.head (builtins.match ".*auto_prepend_file = ([^\n]*).*" cliIni);
      prepend = builtins.readFile path;
    in lib.hasInfix "auto_prepend_file = ${path}\n" system.config.services.phpfpm.pools.wordpress.phpOptions
      && lib.hasInfix "$GLOBALS['wp_filter']['wp_mail_from']" prepend
      && lib.hasInfix ''is_email( $from ) ? $from : "dev@client.test"'' prepend;
    checkSettingsExported = base_.config.environment.etc."wordpress/site.json".source
        == base_.config.systemd.services.wordpress-setup.environment.WP_SITE_SETTINGS
      && (settingsOf base_).mailpit.uiPort == 8025;
    agentIsolationKeepsUrl = valid { extra.nixant.isolation = "agent"; }
      && (evaluate { extra.nixant.isolation = "agent"; }).config.wordpress.url == "http://localhost:8081";
    rootNeedsWorkspace = fails "keep nixant.mounts.workspace enabled" {
      extra.nixant.mounts.workspace.enable = false;
    };
    nothingWhenDisabled = !(evaluate { extra.wordpress.enable = lib.mkForce false; }).config.services.caddy.enable;
    defaultsAreDevelopmentOnly = base.admin.user == "admin" && base.root == "public";

    missingNixant = fails "nixant options are missing" { withNixant = false; extra.wordpress.url = "http://localhost:8081"; };
    parentRoot = fails "must not contain '..'" { extra.wordpress.root = "../outside"; };
    nestedParentRoot = fails "must not contain '..'" { extra.wordpress.root = "a/../../b"; };
    absoluteRoot = fails "relative to the project root" { extra.wordpress.root = "/etc"; };
    emptyRoot = fails "relative to the project root" { extra.wordpress.root = ""; };
    dotsInNamesAllowed = valid { extra.wordpress.root = "sites/v1..2/web"; };
    underivableUrl = fails "cannot be derived" { extra.nixant.ports = lib.mkForce []; };
    nonHttpUrl = fails "must start with http://" { extra.wordpress.url = "https://localhost"; };
    urlWithPath = fails "without a path" { extra.wordpress.url = "http://localhost:8081/site"; };
    urlTrailingSlash = fails "without a path" { extra.wordpress.url = "http://localhost:8081/"; };
    urlBadPort = fails "without a path" { extra.wordpress.url = "http://localhost:99999"; };
    urlPortTakenByMailpit = fails "Mailpit or MariaDB" { extra.wordpress.url = "http://localhost:8025"; };
    urlPortTakenByMariadb = fails "Mailpit or MariaDB" { extra.wordpress.url = "http://localhost:3306"; };
  };
in assert lib.assertMsg (lib.all (value: value) (builtins.attrValues tests))
  "nixant-wp eval tests failed: ${lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) tests))}";
  tests

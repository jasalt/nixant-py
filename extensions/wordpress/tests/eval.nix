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

  base = (evaluate {}).config.wordpress;
  tests = {
    validConfigBuilds = valid {};
    disabledNeedsNothing = succeeds (evaluate {
      withNixant = false;
      extra.wordpress.enable = lib.mkForce false;
    }).config.system.build.toplevel.drvPath;
    urlDerivedFromPort = base.url == "http://localhost:8081";
    urlExplicitWins = (evaluate { extra.wordpress.url = "http://localhost:9000"; }).config.wordpress.url == "http://localhost:9000";
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

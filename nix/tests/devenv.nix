{ nixpkgs, system }:
let
  lib = nixpkgs.lib;
  evaluate = extra: (lib.nixosSystem {
    inherit system;
    modules = [ ../modules/container.nix ../modules/devenv.nix {
      nixant.instanceName = "test-dev";
      system.stateVersion = "25.05";
    } extra ];
  }).config;
  config = evaluate {};
  agent = evaluate { nixant.isolation = "agent"; };
  hasPackage = config: pkg:
    lib.any (p: (p.pname or p.name or "") == pkg) config.environment.systemPackages;
  cache = config: builtins.elem "https://devenv.cachix.org" config.nix.settings.substituters
    && lib.any (lib.hasPrefix "devenv.cachix.org-1:") config.nix.settings.trusted-public-keys
    # Adding the devenv cache must keep the default one.
    && builtins.elem "https://cache.nixos.org/" config.nix.settings.substituters;
  tests = {
    packages = hasPackage config "devenv" && hasPackage config "git";
    cache = cache config;
    agentCache = cache agent;
    agentUntrusted = !(builtins.elem "dev" agent.nix.settings.trusted-users);
    builds = lib.hasSuffix ".drv" agent.system.build.toplevel.drvPath;
    assertions = lib.all (item: item.assertion) (config.assertions ++ agent.assertions);
  };
in assert lib.assertMsg (lib.all (value: value) (builtins.attrValues tests))
  "nixant devenv tests failed: ${lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) tests))}";
  tests

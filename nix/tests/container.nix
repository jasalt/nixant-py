{ nixpkgs, system }:
let
  lib = nixpkgs.lib;
  evaluate = extra: (lib.nixosSystem {
    inherit system;
    modules = [ ../modules/container.nix {
      nixant.instanceName = "test-dev";
      system.stateVersion = "25.05";
    } extra ];
  }).config;
  config = evaluate {};
  tests = {
    container = config.boot.isContainer;
    golden = config.nixant.runtime == builtins.fromJSON (builtins.readFile ./runtime.json);
    hostname = config.networking.hostName == "test-dev";
    hostnameOverride = (evaluate { networking.hostName = "override"; }).networking.hostName == "override";
    network = config.systemd.network.enable && !config.networking.useDHCP &&
      !config.networking.useHostResolvConf &&
      config.systemd.network.networks."10-eth0".networkConfig.DHCP == "yes";
    user = config.users.users.dev.uid == 1000 && config.users.groups.dev.gid == 1000 &&
      builtins.elem "wheel" config.users.users.dev.extraGroups;
    sudo = lib.any (rule: rule.users == [ "dev" ] &&
      lib.any (command: command.command == "ALL" && builtins.elem "NOPASSWD" command.options) rule.commands)
      config.security.sudo.extraRules;
    noSudo = !(lib.any (rule: builtins.elem "dev" rule.users)
      (evaluate { nixant.user.sudo = false; }).security.sudo.extraRules);
    trusted = lib.all (name: builtins.elem name config.nix.settings.trusted-users) [ "root" "dev" ];
    flakes = builtins.elem "flakes" config.nix.settings.experimental-features;
    staleGuard = lib.hasInfix "rm -f /etc/nixos/configuration.nix /etc/nixos/incus.nix"
      config.system.activationScripts.nixant-stale-config.text;
    hostnameActivation = lib.hasInfix "bin/hostname"
      config.system.activationScripts.nixant-hostname.text;
    agentNoPrivileges = let agentConfig = evaluate { nixant.isolation = "agent"; }; in
      !(lib.any (rule: builtins.elem "dev" rule.users) agentConfig.security.sudo.extraRules) &&
      !(builtins.elem "wheel" agentConfig.users.users.dev.extraGroups) &&
      !(builtins.elem "dev" agentConfig.nix.settings.trusted-users) &&
      builtins.elem "root" agentConfig.nix.settings.trusted-users;
    agentLimitDefaults = let runtime = (evaluate { nixant.isolation = "agent"; }).nixant.runtime; in
      runtime.cpus == 2 && runtime.memoryBytes == 4294967296;
    agentLimitOverride = let runtime = (evaluate { nixant.isolation = "agent"; nixant.cpus = 4; nixant.memory = "1GiB"; }).nixant.runtime; in
      runtime.cpus == 4 && runtime.memoryBytes == 1073741824;
    agentExplicitSudoFails = !(lib.all (item: item.assertion)
      (evaluate { nixant.isolation = "agent"; nixant.user.sudo = true; }).assertions);
    assertions = lib.all (item: item.assertion) config.assertions;
  };
in assert lib.assertMsg (lib.all (value: value) (builtins.attrValues tests))
  "nixant container tests failed: ${lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) tests))}";
  tests

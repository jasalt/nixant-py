{ nixpkgs, system }:
let
  lib = nixpkgs.lib;
  evaluate = extra: (lib.nixosSystem {
    inherit system;
    modules = [ ../modules/vm.nix {
      nixant.instanceName = "test-dev";
      system.stateVersion = "25.05";
    } extra ];
  }).config;
  config = evaluate {};
  tests = {
    notContainer = !config.boot.isContainer;
    golden = config.nixant.runtime
      == (builtins.fromJSON (builtins.readFile ./runtime.json) // { kind = "vm"; });
    # Without the agent the CLI loses `incus exec` after the first switch.
    agent = config.virtualisation.incus.agent.enable;
    bootLoader = config.boot.loader.systemd-boot.enable;
    hostname = config.networking.hostName == "test-dev";
    network = config.systemd.network.enable && !config.networking.useDHCP &&
      config.systemd.network.networks."10-nic".matchConfig.Name == "en*" &&
      config.systemd.network.networks."10-nic".networkConfig.DHCP == "ipv4";
    user = config.users.users.dev.uid == 1000 &&
      builtins.elem "wheel" config.users.users.dev.extraGroups;
    flakes = builtins.elem "flakes" config.nix.settings.experimental-features;
    staleGuard = lib.hasInfix "rm -f /etc/nixos/configuration.nix /etc/nixos/incus.nix"
      config.system.activationScripts.nixant-stale-config.text;
    assertions = lib.all (item: item.assertion) config.assertions;
  };
in assert lib.assertMsg (lib.all (value: value) (builtins.attrValues tests))
  "nixant vm tests failed: ${lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) tests))}";
  tests

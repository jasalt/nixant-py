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
    noWayland = config.users.users.dev.linger != true && !(config.environment.sessionVariables ? WAYLAND_DISPLAY);
    wayland = let wl = evaluate { nixant.wayland = true; }; in
      wl.nixant.runtime.wayland && wl.users.users.dev.linger == true &&
      wl.environment.sessionVariables.WAYLAND_DISPLAY == "wayland-0" &&
      builtins.elem "L+ %t/wayland-0 - - - - /dev/nixant-wayland-0"
        wl.systemd.user.tmpfiles.users.dev.rules &&
      lib.hasInfix "XDG_RUNTIME_DIR" wl.environment.extraInit;
    noX11 = !(config.systemd.user.services ? nixant-xwayland) &&
      !(config.environment.sessionVariables ? DISPLAY);
    x11 = let x = evaluate { nixant.wayland = true; nixant.x11 = true; }; in
      x.environment.sessionVariables.DISPLAY == ":0" &&
      x.environment.sessionVariables.WAYLAND_DISPLAY == "wayland-0" &&
      lib.hasInfix "xwayland-satellite :0" x.systemd.user.services.nixant-xwayland.serviceConfig.ExecStart;
    noGpu = !config.hardware.graphics.enable && !(builtins.elem "render" config.users.users.dev.extraGroups);
    gpu = let g = evaluate { nixant.gpu = true; }; in
      g.hardware.graphics.enable && builtins.elem "render" g.users.users.dev.extraGroups &&
      g.nixant.runtime.gpu.gid == g.users.groups.render.gid;
    assertions = lib.all (item: item.assertion) config.assertions;
  };
in assert lib.assertMsg (lib.all (value: value) (builtins.attrValues tests))
  "nixant container tests failed: ${lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) tests))}";
  tests

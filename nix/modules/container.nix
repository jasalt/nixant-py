{ config, lib, pkgs, modulesPath, ... }:
let
  cfg = config.nixant;
  message = "This system is managed by nixant (instance ${cfg.instanceName}). Edit the project flake and run `nixant rebuild` on the host.";
  stub = pkgs.writeText "nixant-configuration.nix" "throw ${builtins.toJSON message}\n";
  readme = pkgs.writeText "nixant-nixos-README" "${message}\n";
in {
  imports = [ ./options.nix "${modulesPath}/virtualisation/lxc-container.nix" ];
  nixant.enable = true;
  networking = {
    hostName = lib.mkDefault cfg.instanceName;
    useDHCP = false;
    useHostResolvConf = false;
  };
  systemd.network = {
    enable = true;
    networks."10-eth0" = {
      matchConfig.Name = "eth0";
      networkConfig.DHCP = "yes";
    };
  };
  users.users.${cfg.user.name} = {
    isNormalUser = true;
    uid = cfg.user.uid;
    group = cfg.user.name;
    home = "/home/${cfg.user.name}";
    extraGroups = [ "wheel" ];
  };
  users.groups.${cfg.user.name}.gid = cfg.user.uid;
  security.sudo.extraRules = lib.mkIf cfg.user.sudo [ {
    users = [ cfg.user.name ];
    commands = [ { command = "ALL"; options = [ "NOPASSWD" ]; } ];
  } ];
  nix.settings = {
    experimental-features = lib.mkDefault [ "nix-command" "flakes" ];
    # NixOS defines root at normal priority; mkDefault here would drop the user.
    trusted-users = [ cfg.user.name ];
  };
  system.activationScripts.nixant-stale-config = {
    deps = [ "etc" ];
    text = ''
      mkdir -p /etc/nixos
      # Remove old files/symlinks before installing; never follow a stock symlink.
      rm -f /etc/nixos/configuration.nix /etc/nixos/incus.nix /etc/nixos/README
      install -m 0644 ${stub} /etc/nixos/configuration.nix
      install -m 0644 ${readme} /etc/nixos/README
    '';
  };
}

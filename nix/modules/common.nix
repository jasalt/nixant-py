{ config, lib, pkgs, ... }:
let
  cfg = config.nixant;
  agent = cfg.isolation == "agent";
  message = "This system is managed by nixant (instance ${cfg.instanceName}). Edit the project flake and run `nixant rebuild` on the host.";
  stub = pkgs.writeText "nixant-configuration.nix" "throw ${builtins.toJSON message}\n";
  readme = pkgs.writeText "nixant-nixos-README" "${message}\n";
in {
  imports = [ ./options.nix ];
  nixant.enable = true;
  networking = {
    hostName = lib.mkDefault cfg.instanceName;
    useDHCP = false;
    useHostResolvConf = false;
  };
  systemd.network.enable = true;
  users.users.${cfg.user.name} = {
    isNormalUser = true;
    uid = cfg.user.uid;
    group = cfg.user.name;
    home = "/home/${cfg.user.name}";
    extraGroups = lib.optional (!agent) "wheel";
    # shell and exec use runuser, which opens no login session; lingering
    # keeps the user manager and /run/user/<uid> around for Wayland.
    linger = lib.mkIf cfg.wayland true;
  };
  users.groups.${cfg.user.name}.gid = cfg.user.uid;
  security.sudo.extraRules = lib.mkIf cfg.user.sudo [ {
    users = [ cfg.user.name ];
    commands = [ { command = "ALL"; options = [ "NOPASSWD" ]; } ];
  } ];
  nix.settings = {
    experimental-features = lib.mkDefault [ "nix-command" "flakes" ];
    # NixOS defines root at normal priority; mkDefault here would drop the user.
    trusted-users = lib.mkIf (!agent) [ cfg.user.name ];
  };
  # The nixant-wayland proxy device listens on /dev/nixant-wayland-0: /dev is
  # the only guest directory that exists before boot, and the runtime dir is a
  # fresh tmpfs per user manager, so it is linked into place from there.
  systemd.user.tmpfiles.users.${cfg.user.name}.rules = lib.mkIf cfg.wayland
    [ "L+ %t/wayland-0 - - - - /dev/nixant-wayland-0" ];
  environment.sessionVariables = lib.mkIf cfg.wayland {
    WAYLAND_DISPLAY = "wayland-0";
    NIXOS_OZONE_WL = "1";
  };
  environment.extraInit = lib.mkIf cfg.wayland ''
    if [ -z "''${XDG_RUNTIME_DIR-}" ] && [ -d "/run/user/$(id -u)" ]; then
      export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    fi
  '';
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
  # Switching does not change the kernel hostname of a running container; the
  # image's hostname would otherwise persist until the next boot.
  system.activationScripts.nixant-hostname = {
    deps = [ "etc" ];
    text = ''
      ${pkgs.nettools}/bin/hostname ${lib.escapeShellArg config.networking.hostName} || true
    '';
  };
}

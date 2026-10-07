{ modulesPath, ... }:
{
  imports = [ ./common.nix "${modulesPath}/virtualisation/lxc-container.nix" ];
  systemd.network.networks."10-eth0" = {
    matchConfig.Name = "eth0";
    networkConfig.DHCP = "yes";
  };
}

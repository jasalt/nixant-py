{ modulesPath, ... }:
{
  # incus-virtual-machine.nix keeps incus-agent enabled (virtualisation.incus.agent);
  # without it the CLI would lose `incus exec` after the first switch.
  imports = [ ./common.nix "${modulesPath}/virtualisation/incus-virtual-machine.nix" ];
  # The NIC name depends on the PCI slot (enp5s0 on stock images), so match by type.
  systemd.network.networks."10-nic" = {
    matchConfig.Name = "en*";
    networkConfig = {
      DHCP = "ipv4";
      IPv6AcceptRA = true;
    };
  };
}

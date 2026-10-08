{ pkgs, ... }:
{
  # devenv and git come from nixant.nixosModules.devenv. The project's own
  # toolchain belongs in devenv.nix, which devenv builds inside the guest.
  environment.systemPackages = with pkgs; [
    gnumake
  ];

  system.stateVersion = "25.05";
}

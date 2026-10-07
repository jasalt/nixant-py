{ pkgs, ... }:
{
  environment.systemPackages = with pkgs; [
    git
    gnumake
  ];

  system.stateVersion = "25.05";
}

{ pkgs, ... }:
{
  environment.systemPackages = with pkgs; [
    git
    gnumake
    nodejs
  ];

  system.stateVersion = "25.05";
}

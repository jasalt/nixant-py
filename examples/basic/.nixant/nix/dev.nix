{ pkgs, ... }:
{
  environment.systemPackages = with pkgs; [
    git
    gnumake
  ];

  services.postgresql.enable = true;

  system.stateVersion = "25.05";
}

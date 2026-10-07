{ pkgs, ... }:
{
  environment.systemPackages = with pkgs; [
    git
    gnumake
    python3
    uv
  ];

  system.stateVersion = "25.05";
}

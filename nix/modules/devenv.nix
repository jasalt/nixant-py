{ pkgs, ... }:
{
  # devenv builds each project's environment inside the guest. The devenv
  # cache is configured in the daemon, not requested by devenv, so it also
  # applies to a user that is not trusted (nixant.isolation = "agent").
  environment.systemPackages = [ pkgs.devenv pkgs.git ];
  nix.settings = {
    substituters = [ "https://devenv.cachix.org" ];
    trusted-public-keys = [ "devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw=" ];
  };
}

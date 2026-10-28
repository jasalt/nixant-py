{ ... }:
{
  # The WordPress stack is declared in ../devenv.nix and built by devenv in the
  # guest; the NixOS side only needs devenv (nixant.nixosModules.devenv).
  system.stateVersion = "25.05";
}

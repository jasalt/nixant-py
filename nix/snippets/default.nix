# Add these inputs to your flake.nix (keep the follows lines so the lock holds one nixpkgs):
#
#   nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
#   nixant.url = "nixant-template-url";
#   nixant.inputs.nixpkgs.follows = "nixpkgs";
#
# Then add this to your outputs:
#
#   nixosConfigurations.dev = nixpkgs.lib.nixosSystem {
#     system = "x86_64-linux";
#     modules = [
#       nixant.nixosModules.container
#       {
#         nixant.instanceName = "nixant-template-dev";
#         system.stateVersion = "25.05";
#       }
#     ];
#   };

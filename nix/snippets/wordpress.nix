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
#       nixant.nixosModules.wordpress
#       {
#         system.stateVersion = "25.05";
#         nixant = {
#           instanceName = "nixant-template-dev";
#           # Must equal the host user's `id -u` so the workspace mount is writable.
#           user.uid = 1000;
#           ports = [
#             { host = 8081; guest = 80; }     # the site; also gives wordpress.url
#             { host = 8025; guest = 8025; }   # Mailpit inbox
#           ];
#         };
#         wordpress.enable = true;
#       }
#     ];
#   };
#
# The site lives in public/; see the template's .gitignore for keeping it out of git.

{
  description = "Isolated WordPress development environments on nixant";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  # Test-only: the module itself does not import nixant, client flakes do.
  # Unpublished for now; switch to github:jasalt/nixant-py once it is.
  inputs.nixant = {
    url = "git+file:///home/user/dev/jail/nixant";
    inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, nixant }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      wp-site = pkgs.callPackage ./nix/wp-site.nix { };
    in {
      nixosModules.wordpress = import ./nix/wordpress.nix;
      nixosModules.default = self.nixosModules.wordpress;

      packages.${system} = {
        inherit wp-site;
        default = wp-site;
      };

      checks.${system} = {
        eval = pkgs.runCommand "nixant-wp-eval-tests" {
          results = builtins.toJSON (import ./tests/eval.nix {
            inherit nixpkgs system nixant;
            wordpress = self.nixosModules.wordpress;
          });
        } ''
          echo "$results" > "$out"
        '';
        wp-site = wp-site;
      };
    };
}

{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixant.url = "github:jasalt/nixant-py";
    nixant.inputs.nixpkgs.follows = "nixpkgs";
    nixant-wp.url = "github:jasalt/nixant-wp";
    nixant-wp.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { nixpkgs, nixant, nixant-wp, ... }: {
    nixosConfigurations.dev = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        nixant.nixosModules.container
        nixant-wp.nixosModules.wordpress
        ./nix/site.nix
      ];
    };
  };
}

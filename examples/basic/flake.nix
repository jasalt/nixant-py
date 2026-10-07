{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixant.url = "path:../..";
    nixant.inputs.nixpkgs.follows = "nixpkgs";
    home-manager.url = "github:nix-community/home-manager";
    home-manager.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { nixpkgs, nixant, home-manager, ... }: {
    nixosConfigurations.dev = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        nixant.nixosModules.container
        home-manager.nixosModules.home-manager
        ./nix/dev.nix
        {
          nixant.instanceName = "shop-dev";
          nixant.user.name = "dev";
          home-manager.users.dev = import ./nix/home.nix;
        }
      ];
    };
  };
}

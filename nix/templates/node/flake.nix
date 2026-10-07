{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixant.url = "nixant-template-url";
    nixant.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { nixpkgs, nixant, ... }: {
    nixosConfigurations.dev = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        nixant.nixosModules.container
        ./nix/dev.nix
        {
          nixant.instanceName = "nixant-template-dev";
        }
      ];
    };
  };
}

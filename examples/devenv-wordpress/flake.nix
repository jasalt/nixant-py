{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixant.url = "path:../..";
    nixant.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { nixpkgs, nixant, ... }:
    let
      ports = import ./ports.nix;
    in {
      nixosConfigurations.dev = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          nixant.nixosModules.container
          nixant.nixosModules.devenv
          ./nix/dev.nix
          {
            nixant.instanceName = "devenv-wordpress-dev";
            # host = guest: nixant's proxy connects to the guest's loopback,
            # where devenv.nix binds these listeners.
            nixant.ports = [
              { host = ports.http; guest = ports.http; }
              { host = ports.mailpit; guest = ports.mailpit; }
            ];
          }
        ];
      };
    };
}

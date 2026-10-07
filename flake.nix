{
  description = "NixOS development environments on local Incus";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      python = pkgs.python3;
    in {
      packages.${system}.default = python.pkgs.buildPythonApplication {
        pname = "nixant";
        version = "0.1.0";
        src = self;
        pyproject = true;
        build-system = [ python.pkgs.hatchling ];
        dependencies = [ python.pkgs.typer ];
        nativeCheckInputs = [ python.pkgs.pytestCheckHook pkgs.git ];
        pythonImportsCheck = [ "nixant.cli" ];
        makeWrapperArgs = [
          "--set NIXANT_SELF ${self}"
          "--set NIXANT_REV '${self.rev or ""}'"
        ];
        meta.mainProgram = "nixant";
      };

      apps.${system}.default = {
        type = "app";
        program = "${self.packages.${system}.default}/bin/nixant";
      };

      nixosModules.container = import ./nix/modules/container.nix;
      nixosModules.options = import ./nix/modules/options.nix;

      checks.${system} = {
        package = self.packages.${system}.default;
        container = pkgs.runCommand "nixant-container-tests" {
          results = builtins.toJSON (import ./nix/tests/container.nix { inherit nixpkgs system; });
        } ''
          echo "$results" > "$out"
        '';
        options = pkgs.runCommand "nixant-options-tests" {
          results = builtins.toJSON (import ./nix/tests/options.nix { inherit nixpkgs system; });
        } ''
          echo "$results" > "$out"
        '';
      };

      devShells.${system}.default = pkgs.mkShell {
        packages = [
          (python.withPackages (ps: [ ps.typer ps.hatchling ps.pytest ]))
          pkgs.ruff
          pkgs.mypy
        ];
        shellHook = ''
          export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
          export NIXANT_SELF="$PWD"
          export NIXANT_REV=""
        '';
      };
    };
}

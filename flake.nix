{
  description = "NixOS development environments on local Incus";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      python = pkgs.python3;
      # Canonical location generated projects depend on. Only clean builds
      # declare it: they have a revision to pin the project's lock to.
      flakeUrl = "github:jasalt/nixant-py";
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
        ] ++ pkgs.lib.optional (self ? rev) "--set NIXANT_FLAKE_URL ${flakeUrl}";
        meta.mainProgram = "nixant";
      };

      apps.${system}.default = {
        type = "app";
        program = "${self.packages.${system}.default}/bin/nixant";
      };

      nixosModules.container = import ./nix/modules/container.nix;
      nixosModules.vm = import ./nix/modules/vm.nix;
      nixosModules.options = import ./nix/modules/options.nix;
      nixosModules.devenv = import ./nix/modules/devenv.nix;

      templates = {
        default = {
          path = ./nix/templates/default;
          description = "Minimal NixOS container with a sudo user";
        };
        node = {
          path = ./nix/templates/node;
          description = "Default container plus Node.js";
        };
        python = {
          path = ./nix/templates/python;
          description = "Default container plus Python 3 and uv";
        };
        devenv = {
          path = ./nix/templates/devenv;
          description = "Default container plus devenv, with a starter devenv.nix";
        };
      };

      checks.${system} = {
        package = self.packages.${system}.default;
        container = pkgs.runCommand "nixant-container-tests" {
          results = builtins.toJSON (import ./nix/tests/container.nix { inherit nixpkgs system; });
        } ''
          echo "$results" > "$out"
        '';
        templates = pkgs.runCommand "nixant-template-tests" {
          results = builtins.toJSON (import ./nix/tests/templates.nix { inherit nixpkgs system; nixant = self; });
        } ''
          echo "$results" > "$out"
        '';
        vm = pkgs.runCommand "nixant-vm-tests" {
          results = builtins.toJSON (import ./nix/tests/vm.nix { inherit nixpkgs system; });
        } ''
          echo "$results" > "$out"
        '';
        devenv = pkgs.runCommand "nixant-devenv-tests" {
          results = builtins.toJSON (import ./nix/tests/devenv.nix { inherit nixpkgs system; });
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

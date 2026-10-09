{ nixpkgs, system, nixant, nixant-wp, template }:
let
  lib = nixpkgs.lib;
  # Call the shipped flake.nix the way Nix does, with inputs substituted.
  callFlake = src: inputs:
    let outputs = (import "${src}/flake.nix").outputs ({ self = outputs // { outPath = src; }; } // inputs);
    in outputs;
  inputs = (import (template + "/flake.nix")).inputs;
  config = (callFlake template { inherit nixpkgs nixant nixant-wp; }).nixosConfigurations.dev.config;
  tests = {
    evaluatesToSystem = lib.hasSuffix ".drv" config.system.build.toplevel.drvPath;
    assertionsHold = lib.all (item: item.assertion) config.assertions;
    wordpressEnabled = config.wordpress.enable;
    urlDerived = config.wordpress.url == "http://localhost:8081";
    mailpitForwarded = lib.any (port: port.guest == 8025) config.nixant.ports;
    inputsFollowNixpkgs = inputs.nixant.inputs.nixpkgs.follows == "nixpkgs"
      && inputs.nixant-wp.inputs.nixpkgs.follows == "nixpkgs"
      && inputs.nixant-wp.inputs.nixant.follows == "nixant";
    rootIsPublic = config.wordpress.root == "public";
    gitignoreKeepsSiteOut = lib.hasInfix "/public/*\n" (builtins.readFile (template + "/.gitignore"));
  };
in assert lib.assertMsg (lib.all (value: value) (builtins.attrValues tests))
  "nixant-wp template tests failed: ${lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) tests))}";
  tests

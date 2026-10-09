{ nixpkgs, system, nixant, template, snippet }:
let
  lib = nixpkgs.lib;
  # Call the shipped flake.nix the way Nix does, with inputs substituted.
  callFlake = src: inputs:
    let outputs = (import "${src}/flake.nix").outputs ({ self = outputs // { outPath = src; }; } // inputs);
    in outputs;
  inputs = (import (template + "/flake.nix")).inputs;
  config = (callFlake template { inherit nixpkgs nixant; }).nixosConfigurations.dev.config;
  site = builtins.readFile (template + "/nix/site.nix");
  snippetText = builtins.readFile snippet;
  tests = {
    evaluatesToSystem = lib.hasSuffix ".drv" config.system.build.toplevel.drvPath;
    assertionsHold = lib.all (item: item.assertion) config.assertions;
    wordpressEnabled = config.wordpress.enable;
    urlDerived = config.wordpress.url == "http://localhost:8081";
    mailpitForwarded = lib.any (port: port.guest == 8025) config.nixant.ports;
    # `nixant init wordpress` substitutes these markers.
    urlMarker = inputs.nixant.url == "nixant-template-url";
    nameMarker = config.nixant.instanceName == "nixant-template-dev"
      && lib.hasInfix ''instanceName = "nixant-template-dev";'' site;
    inputsFollowNixpkgs = inputs.nixant.inputs.nixpkgs.follows == "nixpkgs";
    rootIsPublic = config.wordpress.root == "public";
    gitignoreKeepsSiteOut = lib.hasInfix "/public/*\n" (builtins.readFile (template + "/.gitignore"));
    snippetMarkers = lib.hasInfix ''nixant.url = "nixant-template-url";'' snippetText
      && lib.hasInfix ''instanceName = "nixant-template-dev";'' snippetText
      && lib.hasInfix "nixant.nixosModules.wordpress" snippetText;
  };
in assert lib.assertMsg (lib.all (value: value) (builtins.attrValues tests))
  "wordpress template tests failed: ${lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) tests))}";
  tests

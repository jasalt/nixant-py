{ nixpkgs, system, nixant }:
let
  lib = nixpkgs.lib;
  # Call a shipped flake.nix the way Nix does, with the inputs a lock gives it.
  callFlake = src: inputs:
    let outputs = (import "${src}/flake.nix").outputs ({ self = outputs // { outPath = src; }; } // inputs);
    in outputs;
  lockedInput = lock: name: lock.nodes.${lock.nodes.root.inputs.${name}};
  rootLock = lib.importJSON ../../flake.lock;
  # A generated project as `nixant init` locks it: nixpkgs followed into nixant.
  names = [ "default" "node" "python" "devenv" ];
  templates = lib.genAttrs names (name: let src = ../templates + "/${name}"; in {
    inputs = (import (src + "/flake.nix")).inputs;
    config = (callFlake src { inherit nixpkgs nixant; }).nixosConfigurations.dev.config;
    flake = builtins.readFile (src + "/flake.nix");
    snippet = builtins.readFile (../snippets + "/${name}.nix");
  });
  hasPackage = name: pkg:
    lib.any (p: (p.pname or p.name or "") == pkg)
      templates.${name}.config.environment.systemPackages;
  # The shipped example with its own lock: nixpkgs must be the tool's pin, and
  # home-manager comes from the locked revision with nixpkgs followed.
  exampleSrc = ../../examples/basic;
  exampleLock = lib.importJSON (exampleSrc + "/flake.lock");
  exampleInputs = (import (exampleSrc + "/flake.nix")).inputs;
  homeManager = callFlake (builtins.fetchTree (lockedInput exampleLock "home-manager").locked) { inherit nixpkgs; };
  example = (callFlake exampleSrc { inherit nixpkgs nixant; home-manager = homeManager; }).nixosConfigurations.dev.config;
  # The devenv WordPress example: evaluated only, no golden runtime.
  wordpressSrc = ../../examples/devenv-wordpress;
  wordpressLock = lib.importJSON (wordpressSrc + "/flake.lock");
  wordpress = (callFlake wordpressSrc { inherit nixpkgs nixant; }).nixosConfigurations.dev.config;
  wordpressPorts = import (wordpressSrc + "/ports.nix");
  builds = config: lib.hasSuffix ".drv" config.system.build.toplevel.drvPath;
  contains = needle: text: lib.hasInfix needle text;
  tests = {
    templatesEvaluate = lib.all (t: t.config.nixant.runtime.instanceName == "nixant-template-dev") (lib.attrValues templates);
    templatesBuild = lib.all (t: builds t.config) (lib.attrValues templates);
    templatesAssertions = lib.all (t: lib.all (item: item.assertion) t.config.assertions) (lib.attrValues templates);
    templateMarkers = lib.all (t: contains ''nixant.instanceName = "nixant-template-dev";'' t.flake) (lib.attrValues templates);
    templateUrlMarkers = lib.all (t: t.inputs.nixant.url == "nixant-template-url") (lib.attrValues templates);
    templateFollows = lib.all (t: t.inputs.nixant.inputs.nixpkgs.follows == "nixpkgs") (lib.attrValues templates);
    snippetMarkers = lib.all (t: contains ''nixant.instanceName = "nixant-template-dev";'' t.snippet) (lib.attrValues templates);
    nodePackages = hasPackage "node" "nodejs" && !(hasPackage "default" "nodejs");
    pythonPackages = hasPackage "python" "uv" && hasPackage "python" "python3";
    devenvPackages = hasPackage "devenv" "devenv" && !(hasPackage "default" "devenv");
    devenvFiles = lib.all (file: builtins.pathExists (../templates/devenv + "/${file}"))
      [ "devenv.nix" "devenv.yaml" ".gitignore" ];
    exampleEvaluates = example.nixant.runtime.instanceName == "shop-dev";
    exampleGolden = example.nixant.runtime == lib.importJSON (exampleSrc + "/runtime.json");
    exampleBuilds = builds example;
    exampleAssertions = lib.all (item: item.assertion) example.assertions;
    exampleHomeManager = example.home-manager.users.dev.programs.git.enable;
    exampleFollows = exampleInputs.nixant.inputs.nixpkgs.follows == "nixpkgs"
      && exampleInputs.home-manager.inputs.nixpkgs.follows == "nixpkgs";
    exampleLockedNixpkgs = (lockedInput exampleLock "nixpkgs").locked.rev == (lockedInput rootLock "nixpkgs").locked.rev;
    exampleLockedFollows = (lockedInput exampleLock "nixant").inputs.nixpkgs == [ "nixpkgs" ]
      && (lockedInput exampleLock "home-manager").inputs.nixpkgs == [ "nixpkgs" ];
    wordpressEvaluates = wordpress.nixant.runtime.instanceName == "devenv-wordpress-dev";
    wordpressPorts = wordpress.nixant.runtime.ports == map (port: { host = port; guest = port; address = "127.0.0.1"; })
      [ wordpressPorts.http wordpressPorts.mailpit ];
    wordpressDevenv = lib.any (p: (p.pname or "") == "devenv") wordpress.environment.systemPackages;
    wordpressBuilds = builds wordpress;
    wordpressAssertions = lib.all (item: item.assertion) wordpress.assertions;
    wordpressLockedNixpkgs = (lockedInput wordpressLock "nixpkgs").locked.rev == (lockedInput rootLock "nixpkgs").locked.rev;
  };
in assert lib.assertMsg (lib.all (value: value) (builtins.attrValues tests))
  "nixant template tests failed: ${lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) tests))}";
  tests

{ nixpkgs, system }:
let
  lib = nixpkgs.lib;
  # Templates and the example keep their role module in nix/dev.nix; the
  # instanceName lives in flake.nix, so supply it the way flake.nix does.
  evaluate = devModule: (lib.nixosSystem {
    inherit system;
    modules = [ ../modules/container.nix devModule { nixant.instanceName = "test-dev"; } ];
  }).config;
  names = [ "default" "node" "python" ];
  templates = lib.genAttrs names (name: {
    config = evaluate (../templates + "/${name}/nix/dev.nix");
    flake = builtins.readFile (../templates + "/${name}/flake.nix");
    snippet = builtins.readFile (../snippets + "/${name}.nix");
  });
  hasPackage = name: pkg:
    lib.any (p: (p.pname or p.name or "") == pkg)
      templates.${name}.config.environment.systemPackages;
  example = (lib.nixosSystem {
    inherit system;
    modules = [ ../modules/container.nix ../../examples/basic/nix/dev.nix { nixant.instanceName = "shop-dev"; } ];
  }).config;
  contains = needle: text: lib.hasInfix needle text;
  tests = {
    templatesEvaluate = lib.all (t: t.config.nixant.runtime.instanceName == "test-dev") (lib.attrValues templates);
    templatesAssertions = lib.all (t: lib.all (item: item.assertion) t.config.assertions) (lib.attrValues templates);
    templateMarkers = lib.all (t: contains ''nixant.instanceName = "nixant-template-dev";'' t.flake) (lib.attrValues templates);
    templateUrlMarkers = lib.all (t: contains ''nixant.url = "nixant-template-url";'' t.flake) (lib.attrValues templates);
    templateFollows = lib.all (t: contains ''nixant.inputs.nixpkgs.follows = "nixpkgs";'' t.flake) (lib.attrValues templates);
    snippetMarkers = lib.all (t: contains ''nixant.instanceName = "nixant-template-dev";'' t.snippet) (lib.attrValues templates);
    nodePackages = hasPackage "node" "nodejs" && !(hasPackage "default" "nodejs");
    pythonPackages = hasPackage "python" "uv" && hasPackage "python" "python3";
    exampleEvaluates = example.nixant.runtime.instanceName == "shop-dev";
    exampleGolden = example.nixant.runtime == (builtins.fromJSON (builtins.readFile ../../examples/basic/runtime.json));
    exampleAssertions = lib.all (item: item.assertion) example.assertions;
  };
in assert lib.assertMsg (lib.all (value: value) (builtins.attrValues tests))
  "nixant template tests failed: ${lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) tests))}";
  tests

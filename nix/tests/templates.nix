{ nixpkgs, system }:
let
  lib = nixpkgs.lib;
  # Templates and the example keep their role module in nix/dev.nix; the
  # instanceName lives in flake.nix, so supply it the way flake.nix does.
  evaluate = devModule: (lib.nixosSystem {
    inherit system;
    modules = [ ../modules/container.nix devModule { nixant.instanceName = "test-dev"; } ];
  }).config;
  template = evaluate ../templates/default/nix/dev.nix;
  example = (lib.nixosSystem {
    inherit system;
    modules = [ ../modules/container.nix ../../examples/basic/nix/dev.nix { nixant.instanceName = "shop-dev"; } ];
  }).config;
  markerFor = file: lib.hasInfix ''nixant.instanceName = "nixant-template-dev";'' (builtins.readFile file);
  tests = {
    templateEvaluates = template.nixant.runtime.instanceName == "test-dev";
    templateAssertions = lib.all (item: item.assertion) template.assertions;
    templateMarker = markerFor ../templates/default/flake.nix;
    templateFollows = lib.hasInfix ''nixant.inputs.nixpkgs.follows = "nixpkgs";'' (builtins.readFile ../templates/default/flake.nix);
    snippetMarker = markerFor ../snippets/default.nix;
    exampleEvaluates = example.nixant.runtime.instanceName == "shop-dev";
    exampleGolden = example.nixant.runtime == (builtins.fromJSON (builtins.readFile ../../examples/basic/runtime.json));
    exampleAssertions = lib.all (item: item.assertion) example.assertions;
  };
in assert lib.assertMsg (lib.all (value: value) (builtins.attrValues tests))
  "nixant template tests failed: ${lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) tests))}";
  tests

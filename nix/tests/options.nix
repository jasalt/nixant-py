{ nixpkgs, system }:
let
  lib = nixpkgs.lib;
  evaluate = evaluateWithLib lib;
  evaluateWithLib = moduleLib: extra: (lib.nixosSystem {
    inherit system;
    specialArgs.lib = moduleLib;
    modules = [
      ../modules/options.nix
      ({ pkgs, ... }: {
        boot.isContainer = true;
        system.stateVersion = "25.05";
        nixant = { enable = true; instanceName = "test-dev"; };
        users.users.dev = {
          isNormalUser = true; uid = 1000; group = "dev";
          home = "/home/dev"; shell = pkgs.bashInteractive;
        };
        users.groups.dev.gid = 1000;
      })
      extra
    ];
  }).config.nixant.runtime;
  base = evaluate {};
  succeeds = value: (builtins.tryEval (builtins.deepSeq value true)).success;
  rejects = extra: !succeeds (evaluate extra);
  tests = {
    oldRelease = !succeeds (evaluateWithLib (lib // { trivial = lib.trivial // { release = "24.11"; }; }) {});
    overflowSize = rejects { nixant.memory = "9223372036854775808B"; };
    overflowUnit = rejects { nixant.memory = "9223372036854775807GiB"; };
    golden = base == builtins.fromJSON (builtins.readFile ./runtime.json);
    sizes = let runtime = evaluate { nixant.memory = "4GiB"; nixant.disk = 1000; };
      in runtime.memoryBytes == 4294967296 && runtime.diskBytes == 1000;
    decimalSize = (evaluate { nixant.memory = "4GB"; }).memoryBytes == 4000000000;
    zeroSize = rejects { nixant.memory = "0GiB"; };
    negativeSize = rejects { nixant.memory = -1; };
    badSize = rejects { nixant.memory = "4bananas"; };
    missingName = rejects { nixant.instanceName = lib.mkForce null; };
    invalidNames = lib.all (name: rejects { nixant.instanceName = lib.mkForce name; })
      [ "" "1dev" "-dev" "dev-" "Dev" "foo.bar" (lib.concatStrings (lib.replicate 64 "a")) ];
    singleLetterName = (evaluate { nixant.instanceName = lib.mkForce "a"; }).instanceName == "a";
    rootName = rejects { nixant.user.name = "root"; };
    rootUid = rejects { nixant.user.uid = 0; };
    pathSource = rejects { nixant.mounts.workspace = { source = ../.; target = "/workspace"; }; };
    relativeTarget = rejects { nixant.mounts.workspace = { source = "."; target = "relative"; }; };
    duplicateTargets = rejects { nixant.mounts = {
      a = { source = "."; target = "/same"; }; b = { source = "."; target = "/same"; };
    }; };
    homeFallback = (evaluate { nixant.mounts = {}; }).workdir == "/home/dev";
    explicitWorkdir = (evaluate { nixant.workdir = "/tmp"; }).workdir == "/tmp";
    relativeWorkdir = rejects { nixant.workdir = "relative"; };
    badPort = rejects { nixant.ports = [{ host = 65536; guest = 80; }]; };
    duplicatePorts = rejects { nixant.ports = [ { host = 80; guest = 80; } { host = 80; guest = 81; } ]; };
    portDefault = (builtins.head (evaluate { nixant.ports = [{ host = 8080; guest = 80; }]; }).ports).address == "127.0.0.1";
    readOnly = rejects { nixant.runtime = {}; };
  };
in assert lib.assertMsg (lib.all (value: value) (builtins.attrValues tests))
  "nixant option tests failed: ${lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) tests))}";
  tests

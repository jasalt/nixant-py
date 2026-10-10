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
    oldStableRelease = !succeeds (evaluateWithLib (lib // { trivial = lib.trivial // { release = "25.11"; }; }) {});
    minimumRelease = succeeds (evaluateWithLib (lib // { trivial = lib.trivial // { release = "26.05"; }; }) {});
    overflowSize = rejects { nixant.memory = "9223372036854775808B"; };
    overflowUnit = rejects { nixant.memory = "9223372036854775807GiB"; };
    x11NeedsWayland = rejects { nixant.x11 = true; };
    x11WithWayland = succeeds (evaluate { nixant.wayland = true; nixant.x11 = true; });
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
    homeFallback = (evaluate { nixant.mounts.workspace.enable = false; }).workdir == "/home/dev";
    workspaceSurvivesOtherMounts = let runtime = evaluate { nixant.mounts.data = { source = "/d"; target = "/data"; }; };
      in runtime.mounts ? workspace && runtime.mounts ? data;
    # Force the whole runtime: a partial override must keep the other defaults.
    workspaceCanBeRetargeted = let runtime = evaluate { nixant.mounts.workspace.target = "/code"; };
      in succeeds runtime && runtime.workdir == "/code"
        && runtime.mounts.workspace == { source = "."; target = "/code"; readOnly = false; };
    workspaceCanBeReadOnly = let runtime = evaluate { nixant.mounts.workspace.readOnly = true; };
      in succeeds runtime
        && runtime.mounts.workspace == { source = "."; target = "/workspace"; readOnly = true; };
    workspaceSourceOverride = let runtime = evaluate { nixant.mounts.workspace.source = "sub"; };
      in succeeds runtime && runtime.mounts.workspace.source == "sub"
        && runtime.mounts.workspace.target == "/workspace";
    noWorkspaceMounts = !((evaluate { nixant.mounts.workspace.enable = false; }).mounts ? workspace);
    explicitWorkdir = (evaluate { nixant.workdir = "/tmp"; }).workdir == "/tmp";
    relativeWorkdir = rejects { nixant.workdir = "relative"; };
    badPort = rejects { nixant.ports = [{ host = 65536; guest = 80; }]; };
    duplicatePorts = rejects { nixant.ports = [ { host = 80; guest = 80; } { host = 80; guest = 81; } ]; };
    hostnameDefault = (builtins.head (evaluate { nixant.ports = [{ host = 8080; guest = 80; }]; }).ports).hostname == null;
    hostnameKept = (builtins.head (evaluate { nixant.ports = [{ host = 8080; guest = 80; hostname = "a.localhost"; }]; }).ports).hostname == "a.localhost";
    duplicateHostnames = rejects { nixant.ports = [ { host = 8080; guest = 80; hostname = "a.localhost"; } { host = 8081; guest = 81; hostname = "a.localhost"; } ]; };
    badHostname = rejects { nixant.ports = [{ host = 8080; guest = 80; hostname = "A_b.localhost"; }]; };
    portDefault = (builtins.head (evaluate { nixant.ports = [{ host = 8080; guest = 80; }]; }).ports).address == "127.0.0.1";
    ephemeral = (evaluate { nixant.ephemeral = true; }).ephemeral && !base.ephemeral;
    agentWritableExtraMount = rejects { nixant.isolation = "agent"; nixant.mounts.data = { source = "/d"; target = "/data"; }; };
    agentReadOnlyExtraMount = succeeds (evaluate { nixant.isolation = "agent"; nixant.mounts.data = { source = "/d"; target = "/data"; readOnly = true; }; });
    agentWorkspaceStaysWritable = !(evaluate { nixant.isolation = "agent"; }).mounts.workspace.readOnly;
    agentLoopbackPorts = succeeds (evaluate { nixant.isolation = "agent"; nixant.ports = [{ host = 8080; guest = 80; }]; });
    agentExplicitLoopback = succeeds (evaluate { nixant.isolation = "agent"; nixant.ports = [{ host = 8080; guest = 80; address = "127.0.0.1"; }]; });
    agentExposedPorts = rejects { nixant.isolation = "agent"; nixant.ports = [{ host = 8080; guest = 80; address = "0.0.0.0"; }]; };
    agentMixedPorts = rejects { nixant.isolation = "agent"; nixant.ports = [{ host = 8080; guest = 80; } { host = 8081; guest = 81; address = "192.168.1.5"; }]; };
    agentSudo = rejects { nixant.isolation = "agent"; nixant.user.sudo = true; };
    badIsolation = rejects { nixant.isolation = "paranoid"; };
    readOnly = rejects { nixant.runtime = {}; };
  };
in assert lib.assertMsg (lib.all (value: value) (builtins.attrValues tests))
  "nixant option tests failed: ${lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) tests))}";
  tests

{ config, lib, ... }:
let
  inherit (lib) mkOption types;
  cfg = config.nixant;
  absolute = value: lib.hasPrefix "/" value;
  sizeUnits = { B = 1; KB = 1000; MB = 1000000; GB = 1000000000; TB = 1000000000000;
    KiB = 1024; MiB = 1048576; GiB = 1073741824; TiB = 1099511627776; };
  parseSize = value:
    if value == null || builtins.isInt value then value
    else let match = builtins.match "([0-9]+)(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)" value;
    in if match == null then throw "nixant: invalid size '${value}'; use integer bytes or e.g. 4GiB"
    else let
      digits = lib.stringToCharacters (builtins.elemAt match 0);
      number = lib.foldl' (n: digit: let d = lib.toInt digit; in
        if n > (9223372036854775807 - d) / 10 then throw "nixant: size exceeds signed 64-bit integer range"
        else n * 10 + d) 0 digits;
      unit = sizeUnits.${builtins.elemAt match 1};
    in if number <= 0 || number > 9223372036854775807 / unit
      then throw "nixant: size must be positive and fit in a signed 64-bit integer"
      else number * unit;
  sizeType = types.nullOr (types.either types.ints.positive types.str);
  mountType = types.submodule {
    options = {
      source = mkOption {
        type = types.addCheck types.str builtins.isString;
        description = "Host source as a string (not a Nix path such as ./.); relative to the project root.";
      };
      target = mkOption { type = types.str; description = "Absolute guest mount path."; };
      readOnly = mkOption { type = types.bool; default = false; };
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "Set to false to drop a mount, e.g. the default workspace.";
      };
    };
  };
  portType = types.submodule {
    options = {
      host = mkOption { type = types.port; };
      guest = mkOption { type = types.port; };
      address = mkOption { type = types.str; default = "127.0.0.1"; };
      hostname = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "mysite.localhost";
        description = ''
          Name that routes to this forward through the host-side proxy, so the
          URL needs no port. Recorded on the instance by `nixant up`.
        '';
      };
    };
  };
  logNames = builtins.attrNames cfg.logs;
  hostnames = builtins.filter (name: name != null) (map (port: port.hostname) cfg.ports);
  hostnamePattern = "[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*";
  mounts = lib.filterAttrs (_: mount: mount.enable) cfg.mounts;
  targets = map (mount: mount.target) (builtins.attrValues mounts);
  hostPorts = map (port: port.host) cfg.ports;
  agent = cfg.isolation == "agent";
  nonLoopbackPorts = builtins.filter (port: port.address != "127.0.0.1") cfg.ports;
  extraWritableMounts = builtins.attrNames
    (lib.filterAttrs (name: mount: name != "workspace" && !mount.readOnly) mounts);
  validations = [
    { assertion = cfg.instanceName != null; message = "Set nixant.instanceName = \"my-project-dev\" in your NixOS configuration."; }
    { assertion = cfg.instanceName == null ||
        (builtins.stringLength cfg.instanceName <= 63 &&
         builtins.match "[a-z]([a-z0-9-]*[a-z0-9])?" cfg.instanceName != null);
      message = "nixant.instanceName must be 1-63 lowercase letters, digits or dashes, start with a letter and not end with a dash."; }
    { assertion = cfg.user.name != "root" && cfg.user.uid != 0;
      message = "nixant.user must be non-root (name != root and uid != 0)."; }
    { assertion = lib.all absolute targets; message = "nixant.mounts targets must be absolute paths."; }
    { assertion = builtins.length targets == builtins.length (lib.unique targets);
      message = "nixant.mounts targets must be unique."; }
    { assertion = cfg.workdir == null || absolute cfg.workdir;
      message = "nixant.workdir must be an absolute path."; }
    { assertion = builtins.length hostPorts == builtins.length (lib.unique hostPorts);
      message = "nixant.ports host ports must be unique."; }
    { assertion = builtins.length hostnames == builtins.length (lib.unique hostnames);
      message = "nixant.ports hostnames must be unique."; }
    { assertion = lib.all (name: builtins.match hostnamePattern name != null) hostnames;
      message = "nixant.ports hostnames must be lowercase DNS names such as mysite.localhost."; }
    { assertion = !agent || !cfg.user.sudo;
      message = "nixant.isolation = \"agent\" forbids nixant.user.sudo; remove the override."; }
    { assertion = !agent || extraWritableMounts == [];
      message = "nixant.isolation = \"agent\" only allows writing to the workspace mount; make these read-only: ${lib.concatStringsSep ", " extraWritableMounts}."; }
    { assertion = !agent || nonLoopbackPorts == [];
      message = "nixant.isolation = \"agent\" only publishes loopback ports; set address = \"127.0.0.1\" or remove these nixant.ports entries: ${lib.concatStringsSep ", " (map (port: "${port.address}:${toString port.host}") nonLoopbackPorts)}."; }
    { assertion = lib.all (name: builtins.match "[a-z0-9][a-z0-9_.-]*" name != null) logNames;
      message = "nixant.logs names must be lowercase letters, digits, dots, dashes or underscores."; }
    { assertion = lib.all absolute (builtins.attrValues cfg.logs);
      message = "nixant.logs paths must be absolute guest paths."; }
    { assertion = !cfg.x11 || cfg.wayland;
      message = "nixant.x11 runs X11 apps on the Wayland socket; also set nixant.wayland = true."; }
    { assertion = lib.versionAtLeast lib.trivial.release "26.05";
      message = "nixant requires nixpkgs 26.05 or newer: the bootstrap image is newer, and switching a guest down to an older release hangs in switch-to-configuration."; }
  ];
  errors = map (item: item.message) (builtins.filter (item: !item.assertion) validations);
  user = config.users.users.${cfg.user.name};
in {
  options.nixant = {
    enable = mkOption { type = types.bool; default = false; };
    instanceName = mkOption { type = types.nullOr types.str; default = null; };
    user = {
      name = mkOption { type = types.str; default = "dev"; };
      uid = mkOption { type = types.ints.unsigned; default = 1000; };
      # Explicit values win; an explicit true under isolation = "agent" is rejected.
      sudo = mkOption { type = types.bool; default = !agent; };
    };
    isolation = mkOption {
      type = types.enum [ "none" "agent" ];
      default = "none";
      description = ''
        "agent" restricts the guest for autonomous coding agents: no sudo, no
        wheel membership, not a trusted Nix user, only the workspace mount
        writable, host ports only on 127.0.0.1, and default CPU/memory caps.
      '';
    };
    cpus = mkOption { type = types.nullOr types.ints.positive; default = if agent then 2 else null; };
    memory = mkOption { type = sizeType; default = if agent then "4GiB" else null; apply = parseSize; };
    disk = mkOption { type = sizeType; default = null; apply = parseSize; };
    mounts = mkOption {
      type = types.attrsOf mountType;
      default = {};
      description = "Host mounts; `workspace` (the project root at /workspace) is defined by default.";
    };
    workdir = mkOption { type = types.nullOr types.str; default = null; };
    ephemeral = mkOption {
      type = types.bool;
      default = false;
      description = "Create the instance as ephemeral: Incus deletes it when it stops.";
    };
    ports = mkOption { type = types.listOf portType; default = []; };
    logs = mkOption {
      type = types.attrsOf types.str;
      default = {};
      example = { debug = "/workspace/log/debug.log"; };
      description = ''
        Log files that `nixant logs NAME` shows, by name, as absolute guest
        paths. Recorded on the instance by `nixant up`.
      '';
    };
    wayland = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Show guest windows on the host: the host's Wayland socket is proxied
        to the guest user as wayland-0. Containers only.
      '';
    };
    x11 = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Run X11-only apps as DISPLAY=:0 through xwayland-satellite in the
        guest, on top of the Wayland socket. Needs wayland; the host's X
        server is never shared.
      '';
    };
    gpu = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Share the host's first GPU render node (/dev/dri/renderD*) for
        hardware rendering. Containers only.
      '';
    };
    runtime = mkOption {
      type = types.attrs;
      readOnly = true;
      internal = true;
      description = "Normalized runtime schema consumed by the nixant CLI.";
    };
  };
  config = lib.mkIf cfg.enable {
    # A default on the option itself would vanish as soon as another mount is
    # added, and one on the whole attrset as soon as one of its keys is set.
    nixant.mounts.workspace = {
      source = lib.mkDefault ".";
      target = lib.mkDefault "/workspace";
    };
    assertions = validations;
    nixant.runtime = if errors != [] then throw (lib.concatStringsSep "\n" errors) else {
      schemaVersion = 1;
      kind = if config.boot.isContainer then "container" else "vm";
      inherit (cfg) instanceName cpus ephemeral;
      memoryBytes = cfg.memory;
      diskBytes = cfg.disk;
      mounts = lib.mapAttrs (_: mount: { inherit (mount) source target readOnly; }) mounts;
      ports = map (port: { inherit (port) host guest address hostname; }) cfg.ports;
      inherit (cfg) wayland logs;
      gpu = if cfg.gpu then { gid = config.users.groups.render.gid; } else null;
      workdir = if cfg.workdir != null then cfg.workdir
        else if mounts ? workspace then mounts.workspace.target else user.home;
      user = {
        inherit (cfg.user) name uid;
        gid = config.users.groups.${user.group}.gid;
        inherit (user) home;
        shell = "/run/current-system/sw${user.shell.shellPath}";
      };
    };
  };
}

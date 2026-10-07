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
    };
  };
  portType = types.submodule {
    options = {
      host = mkOption { type = types.port; };
      guest = mkOption { type = types.port; };
      address = mkOption { type = types.str; default = "127.0.0.1"; };
    };
  };
  targets = map (mount: mount.target) (builtins.attrValues cfg.mounts);
  hostPorts = map (port: port.host) cfg.ports;
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
    { assertion = lib.versionAtLeast lib.trivial.release "25.05";
      message = "nixant requires nixpkgs 25.05 or newer (switch-to-configuration-ng)."; }
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
      sudo = mkOption { type = types.bool; default = true; };
    };
    cpus = mkOption { type = types.nullOr types.ints.positive; default = null; };
    memory = mkOption { type = sizeType; default = null; apply = parseSize; };
    disk = mkOption { type = sizeType; default = null; apply = parseSize; };
    mounts = mkOption {
      type = types.attrsOf mountType;
      default.workspace = { source = "."; target = "/workspace"; };
    };
    workdir = mkOption { type = types.nullOr types.str; default = null; };
    ports = mkOption { type = types.listOf portType; default = []; };
    runtime = mkOption {
      type = types.attrs;
      readOnly = true;
      internal = true;
      description = "Normalized runtime schema consumed by the nixant CLI.";
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = validations;
    nixant.runtime = if errors != [] then throw (lib.concatStringsSep "\n" errors) else {
      schemaVersion = 1;
      kind = if config.boot.isContainer then "container" else "vm";
      inherit (cfg) instanceName cpus;
      memoryBytes = cfg.memory;
      diskBytes = cfg.disk;
      mounts = lib.mapAttrs (_: mount: { inherit (mount) source target readOnly; }) cfg.mounts;
      ports = map (port: { inherit (port) host guest address; }) cfg.ports;
      workdir = if cfg.workdir != null then cfg.workdir
        else if cfg.mounts ? workspace then cfg.mounts.workspace.target else user.home;
      user = {
        inherit (cfg.user) name uid;
        gid = config.users.groups.${user.group}.gid;
        inherit (user) home;
        shell = "/run/current-system/sw${user.shell.shellPath}";
      };
    };
  };
}

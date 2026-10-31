{ config, lib, pkgs, ... }:
let
  cfg = config.nixant;
  # Never load user-controlled configuration or expose a root terminal launcher.
  westonConfig = pkgs.writeText "nixant-weston.ini" ''
    [core]
    idle-time=0
    [shell]
    locking=false
    panel-position=none
    background-color=0xff000000
    [launcher]
    icon=${pkgs.weston}/share/weston/terminal.png
    path=${pkgs.coreutils}/bin/true
    displayname=Disabled
    [input-method]
    path=
  '';
in {
  config = lib.mkIf (cfg.wayland && cfg.desktop.mode == "nested") {
    systemd.tmpfiles.rules = [
      "d /run/nixant-desktop 0750 root ${cfg.user.name} -"
    ];
    systemd.services.nixant-weston = {
      description = "Private nested Weston desktop for ${cfg.instanceName}";
      wantedBy = [ "multi-user.target" ];
      after = [ "systemd-tmpfiles-setup.service" ];
      # Keep retrying when the host display is temporarily unavailable.
      unitConfig.StartLimitIntervalSec = 0;
      environment = {
        XDG_RUNTIME_DIR = "/run/nixant-weston";
        WAYLAND_DISPLAY = "/dev/nixant-wayland-0";
        HOME = "/run/nixant-weston";
      };
      serviceConfig = {
        User = "root";
        RuntimeDirectory = "nixant-weston";
        RuntimeDirectoryMode = "0700";
        UMask = "0077";
        ExecStart = lib.concatStringsSep " " [
          "${pkgs.weston}/bin/weston"
          "--backend=wayland"
          "--config=${westonConfig}"
          "--socket=/run/nixant-desktop/wayland-0"
          "--width=${toString cfg.desktop.width}"
          "--height=${toString cfg.desktop.height}"
          "--output-count=1"
          (lib.optionalString (!cfg.gpu) "--renderer=pixman")
        ];
        ExecStartPost = "${pkgs.writeShellScript "nixant-weston-ready" ''
          set -eu
          socket=/run/nixant-desktop/wayland-0
          for attempt in $(${pkgs.coreutils}/bin/seq 1 100); do
            if [ -S "$socket" ]; then
              ${pkgs.coreutils}/bin/chown root:${lib.escapeShellArg cfg.user.name} "$socket"
              ${pkgs.coreutils}/bin/chmod 0660 "$socket"
              exit 0
            fi
            ${pkgs.coreutils}/bin/sleep 0.1
          done
          echo "Weston did not create its application socket" >&2
          exit 1
        ''}";
        ExecStopPost = "${pkgs.coreutils}/bin/rm -f /run/nixant-desktop/wayland-0 /run/nixant-desktop/wayland-0.lock";
        Restart = "always";
        RestartSec = 5;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        ReadWritePaths = [ "/run/nixant-desktop" ];
        CapabilityBoundingSet = [ "CAP_CHOWN" ];
        RestrictSUIDSGID = true;
      };
    };
  };
}

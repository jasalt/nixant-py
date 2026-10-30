{ pkgs }:
pkgs.testers.runNixOSTest {
  name = "nixant-nested-desktop";
  nodes.machine = { lib, ... }: {
    imports = [ ../modules/common.nix ];
    nixant.instanceName = "machine";
    nixant.wayland = true;
    system.stateVersion = "25.05";
    # Simulate the Incus upstream socket using a root-only headless compositor.
    systemd.services.host-display = {
      wantedBy = [ "multi-user.target" ];
      environment.XDG_RUNTIME_DIR = "/run/host-display";
      serviceConfig = {
        RuntimeDirectory = "host-display";
        RuntimeDirectoryMode = "0700";
        UMask = "0077";
        ExecStart = "${pkgs.weston}/bin/weston --backend=headless --renderer=pixman --no-config --socket=/dev/nixant-wayland-0 --idle-time=0";
        Restart = "always";
      };
    };
    systemd.services.nixant-weston.after = [ "host-display.service" ];
    environment.systemPackages = [ pkgs.python3 pkgs.wayland-utils ];
    # Do not let the test framework's autologin imply an unlocked dev password.
    users.users.dev.hashedPassword = lib.mkForce "!";
  };
  testScript = ''
    start_all()
    machine.wait_for_unit("nixant-weston.service")
    machine.wait_for_unit("user@1000.service")
    machine.succeed("test $(stat -c %u:%g:%a /dev/nixant-wayland-0) = 0:0:700")
    machine.succeed("test $(stat -c %u:%g:%a /run/nixant-desktop/wayland-0) = 0:1000:660")
    machine.fail("runuser -u dev -- sudo -n true")
    machine.succeed("! id -nG dev | grep -w wheel")
    machine.succeed("grep -Fx 'trusted-users = root' /etc/nix/nix.conf")
    machine.fail("runuser -u dev -- python3 -c \"import socket; socket.socket(socket.AF_UNIX).connect('/dev/nixant-wayland-0')\"")
    machine.fail("runuser -u dev -- touch /run/nixant-weston/forbidden")
    machine.fail("runuser -u dev -- rm /run/nixant-desktop/wayland-0")
    client = "runuser -u dev -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 wayland-info"
    machine.wait_until_succeeds(client)
    machine.succeed("systemctl restart nixant-weston")
    machine.wait_until_succeeds(client)
    machine.succeed("systemctl stop host-display")
    machine.succeed("systemctl start host-display")
    machine.wait_until_succeeds(client)
  '';
}

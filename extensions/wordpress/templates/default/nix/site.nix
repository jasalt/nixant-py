# Settings for this client's WordPress site. Edit, then run `nixant up`.
{ ... }:
{
  system.stateVersion = "25.05";

  nixant = {
    # A unique name per client: it names the Incus instance.
    instanceName = "client-dev";
    # Must equal the host user's `id -u` so the workspace mount is writable.
    user.uid = 1000;
    ports = [
      { host = 8081; guest = 80; }     # the site; also gives wordpress.url
      { host = 8025; guest = 8025; }   # Mailpit inbox
    ];
  };

  wordpress = {
    enable = true;
    title = "Client site";
    # Link plugins and themes from this repository into the site; paths are
    # relative to the project root.
    # plugins.my-plugin.path = "plugins/my-plugin";
    # themes.my-theme.path = "themes/my-theme";
    # activeTheme = "my-theme";
  };
}

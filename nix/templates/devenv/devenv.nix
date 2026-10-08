# Evaluated by devenv inside the guest: run `nixant shell`, then `devenv shell`
# or `devenv up`. Do not run devenv on the host in this directory; .devenv/
# holds the guest's store paths and service state.
{ pkgs, ... }:
{
  # https://devenv.sh/packages/
  packages = [ pkgs.git ];

  # https://devenv.sh/languages/
  # languages.python.enable = true;

  # https://devenv.sh/services/
  # services.postgres.enable = true;

  # https://devenv.sh/processes/
  # processes.web.exec = "python -m http.server 8000";

  enterShell = ''
    echo "devenv shell in $DEVENV_ROOT"
  '';
}

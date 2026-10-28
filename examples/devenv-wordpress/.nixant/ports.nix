# Plain data shared by flake.nix (nixant.ports) and devenv.nix (listeners), so
# the forwarded ports and the services' ports cannot drift apart. They differ
# from nixant-wp's defaults (8081, 8025), so both can run side by side.
{
  http = 8090;
  mailpit = 8026;
}

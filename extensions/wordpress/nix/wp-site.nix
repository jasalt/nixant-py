{ writeShellApplication, coreutils, curl, jq, mariadb, rsync, wp-cli }:
writeShellApplication {
  name = "wp-site";
  runtimeInputs = [ coreutils curl jq mariadb.client rsync wp-cli ];
  text = builtins.readFile ./wp-site.sh;
}

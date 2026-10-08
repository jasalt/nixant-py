{ writeShellApplication, coreutils, curl, findutils, jq, mariadb, rsync, wp-cli }:
writeShellApplication {
  name = "wp-site";
  runtimeInputs = [ coreutils curl findutils jq mariadb.client rsync wp-cli ];
  text = builtins.readFile ./wp-site.sh;
}

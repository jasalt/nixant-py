{ lib, writeShellApplication }:
writeShellApplication {
  name = "wp-site";
  runtimeInputs = [ ];
  text = builtins.readFile ./wp-site.sh;
}

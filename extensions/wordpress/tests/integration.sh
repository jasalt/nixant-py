#!/usr/bin/env bash
# Opt-in integration test: the whole MVP workflow against a real Incus.
#
# Creates two throwaway clients from the template, brings both up in parallel
# with `nixant up`, checks them, checks that the site's files are on the host,
# edits a plugin on the host and destroys both.
# Instances are named nixwp-it-<pid>-{a,b} and removed on every exit path.
#
# Needs Nix with flakes, Incus (your user in incus-admin) and a nixant
# checkout: NIXANT_SRC defaults to ../nixant. Do not edit that checkout while
# this runs, since nixant is built from its working tree.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
nixant_src=$(cd "${NIXANT_SRC:-$repo/../nixant}" && pwd)
work=$(mktemp -d)
prefix="nixwp-it-$$"
clients=(a b)
declare -A ports=([a]=8081 [b]=8082)
declare -A ui_ports=([a]=8025 [b]=8026)

# Fetch a page and require a pattern. `curl | grep -q` would fail under
# pipefail whenever grep exits before curl has finished writing.
page_has() {
  local port=$1 pattern=$2 body
  body=$(curl -fs "http://localhost:$port/") || return 1
  grep -q -- "$pattern" <<<"$body"
}

step() { printf '\n==> %s\n' "$*"; }
fail() { echo "integration: FAIL: $*" >&2; exit 1; }

cleanup() {
  local status=$? client
  trap - EXIT
  for client in "${clients[@]}"; do
    if [ -d "$work/$client" ]; then
      (cd "$work/$client" && "$nixant" destroy "dev" >/dev/null 2>&1) || true
    fi
  done
  # Anything left over by an interrupted nixant run.
  for name in $(incus list -c n --format csv 2>/dev/null | grep "^$prefix-" || true); do
    incus delete --force "$name" >/dev/null 2>&1 || true
  done
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

step "build nixant from $nixant_src"
nixant=$(nix build --no-link --print-out-paths "path:$nixant_src")/bin/nixant

in_client() { local client=$1; shift; (cd "$work/$client" && "$@"); }
ncli() { local client=$1; shift; in_client "$client" "$nixant" "$@"; }

make_client() {
  local client=$1 dir="$work/$1"
  mkdir -p "$dir/public/wp-content/plugins/it-plugin"
  (
    cd "$dir"
    git init -q
    nix flake init -t "path:$repo" >/dev/null
    sed -i \
      -e "s/instanceName = \"client-dev\"/instanceName = \"$prefix-$client\"/" \
      -e "s/user.uid = 1000/user.uid = $(id -u)/" \
      -e "s/host = 8081;/host = ${ports[$client]};/" \
      -e "s/host = 8025;/host = ${ui_ports[$client]};/" \
      -e "s/title = \"Client site\"/title = \"Client $client\"/" \
      nix/site.nix
    cat >public/wp-content/plugins/it-plugin/it-plugin.php <<PHP
<?php
/* Plugin Name: Integration plugin */
add_action( 'wp_footer', function () { echo '<!-- it-plugin v1 -->'; } );
PHP
    git add -A
    nix flake lock \
      --override-input nixant "path:$nixant_src" \
      --override-input nixant-wp "path:$repo" >/dev/null 2>&1
    git add -A
  )
}

step "create clients ${clients[*]}"
for client in "${clients[@]}"; do make_client "$client"; done

step "nixant up for both clients in parallel"
pids=()
for client in "${clients[@]}"; do
  ncli "$client" up >"$work/$client.up.log" 2>&1 &
  pids+=($!)
done
for index in "${!clients[@]}"; do
  wait "${pids[$index]}" || { cat "$work/${clients[$index]}.up.log" >&2; fail "nixant up failed for client ${clients[$index]}"; }
done
for client in "${clients[@]}"; do
  grep -q "activated with failed units" "$work/$client.up.log" && fail "client $client is degraded"
done

step "wp-site check on both clients"
for client in "${clients[@]}"; do
  ncli "$client" exec -- wp-site check || fail "wp-site check failed for client $client"
  ncli "$client" exec -- wp plugin activate it-plugin >/dev/null || fail "client $client cannot activate its plugin"
done

step "the site's files are on the host, owned by the host user"
for client in "${clients[@]}"; do
  [ -e "$work/$client/public/wp-includes/version.php" ] || fail "client $client has no core on the host"
  [ "$(stat -c %u "$work/$client/public/wp-config.php")" = "$(id -u)" ] \
    || fail "client $client: wp-config.php is not owned by the host user"
done

step "clients are isolated"
for client in "${clients[@]}"; do
  [ "$(ncli "$client" exec -- wp option get blogname)" = "Client $client" ] || fail "client $client has the wrong title"
  [ "$(ncli "$client" exec -- wp option get home)" = "http://localhost:${ports[$client]}" ] || fail "client $client has the wrong home URL"
  page_has "${ports[$client]}" "<title>Client $client" || fail "client $client does not serve its own site"
done

step "host edit of a plugin is live without nixant up"
page_has "${ports[a]}" 'it-plugin v1' || fail "plugin output v1 missing"
sed -i 's/it-plugin v1/it-plugin v2/' "$work/a/public/wp-content/plugins/it-plugin/it-plugin.php"
page_has "${ports[a]}" 'it-plugin v2' || fail "host edit did not show up"

step "rerunning nixant up is a no-op"
invocation() { ncli a exec -- systemctl show wordpress-setup -p InvocationID --value; }
before=$(invocation)
ncli a up >"$work/a.up2.log" 2>&1 || { cat "$work/a.up2.log" >&2; fail "second nixant up failed"; }
[ "$(invocation)" = "$before" ] || fail "second nixant up reran wordpress-setup"

step "destroy"
for client in "${clients[@]}"; do ncli "$client" destroy "dev" >/dev/null; done
if incus list -c n --format csv | grep -q "^$prefix-"; then
  fail "instances left behind"
fi

step "integration: OK"

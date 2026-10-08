# wp-site: setup and check logic for the nixant-wp WordPress site.
#
# Everything site-specific comes from the JSON file named by WP_SITE_SETTINGS,
# which the NixOS module generates. The web root is a project directory on the
# host, and WordPress owns it: `setup` only creates what is missing (core,
# wp-config.php, the installation) and keeps the database settings and the URL
# current. It is rerun whenever the settings change and must be idempotent.

die() {
  echo "wp-site: $*" >&2
  exit 1
}

setting() {
  jq -er "$1" "$WP_SITE_SETTINGS"
}

load_settings() {
  # The module exports the generated settings at this path for manual runs.
  WP_SITE_SETTINGS=${WP_SITE_SETTINGS:-/etc/wordpress/site.json}
  [ -r "$WP_SITE_SETTINGS" ] || die "settings file $WP_SITE_SETTINGS is not readable"
  root=$(setting .root)
  core=$(setting .core)
  url=$(setting .url)
  title=$(setting .title)
  admin_user=$(setting .admin.user)
  admin_password=$(setting .admin.password)
  admin_email=$(setting .admin.email)
  mailpit_ui_port=$(setting .mailpit.uiPort)
  db_name=$(setting .db.name)
  db_user=$(setting .db.user)
  db_socket=$(setting .db.socket)
  mailpit_url=$(setting '.mailpitUrl // "not forwarded to the host"')
  http_address=$(setting .httpAddress)
}

wp() {
  command wp --path="$root" "$@"
}

# Copy the core into a web root that has none. Files already there, such as a
# wp-content from git, are kept; afterwards WordPress updates its own files.
seed_core() {
  if [ -e "$root/wp-includes/version.php" ]; then
    return
  fi
  [ -e "$core/wp-includes/version.php" ] || die "$core is not a WordPress core (no wp-includes/version.php)"
  echo "wp-site: copying WordPress core into $root"
  mkdir -p "$root"
  rsync -rlp --chmod=D755,F644 --ignore-existing "$core"/ "$root"/
}

# Set a wp-config.php constant only when it differs, so an unchanged file is
# not rewritten under the host's editor.
config_ensure() {
  if [ "$(wp config get "$1" 2>/dev/null || true)" != "$2" ]; then
    wp config set "$1" "$2"
  fi
}

configure() {
  if [ ! -e "$root/wp-config.php" ]; then
    wp config create --skip-check --dbname="$db_name" --dbuser="$db_user" \
      --dbpass='' --dbhost="localhost:$db_socket"
    # Development defaults, written once: wp-config.php is yours afterwards.
    wp config set WP_DEBUG true --raw
    wp config set WP_DEBUG_LOG true --raw
    wp config set WP_DEBUG_DISPLAY false --raw
    wp config set WP_ENVIRONMENT_TYPE local
    # Core, plugins and themes change when you update them, not in the background.
    wp config set AUTOMATIC_UPDATER_DISABLED true --raw
  fi
  # Always the guest's database, also in a wp-config.php copied from elsewhere.
  config_ensure DB_NAME "$db_name"
  config_ensure DB_USER "$db_user"
  config_ensure DB_PASSWORD ''
  config_ensure DB_HOST "localhost:$db_socket"
}

wait_for_database() {
  local seconds=0
  while [ "$seconds" -lt 60 ]; do
    if mariadb --socket="$db_socket" --user="$db_user" -e 'SELECT 1' "$db_name" >/dev/null 2>&1; then
      return
    fi
    sleep 1
    seconds=$((seconds + 1))
  done
  die "database $db_name is not reachable as $db_user on $db_socket after 60s"
}

install_site() {
  if wp core is-installed 2>/dev/null; then
    return
  fi
  wp core install --url="$url" --title="$title" --admin_user="$admin_user" \
    --admin_password="$admin_password" --admin_email="$admin_email" --skip-email
  wp rewrite structure '/%postname%/'
}

# Keep home and siteurl at the configured URL, for example after the forwarded
# host port changed.
converge_urls() {
  local option
  for option in home siteurl; do
    if [ "$(wp option get "$option")" != "$url" ]; then
      wp option update "$option" "$url"
    fi
  done
}

report() {
  cat <<REPORT
WordPress is ready:
  site:    $url
  admin:   $url/wp-admin/  ($admin_user / $admin_password unless changed; development only)
  mailpit: $mailpit_url
  files:   $root
REPORT
}

setup() {
  load_settings
  set -o errtrace # so the trap also covers the functions below
  trap 'echo "wp-site: setup failed at: $BASH_COMMAND" >&2' ERR
  seed_core
  configure
  wait_for_database
  install_site
  converge_urls
  report
}

# check: verify a running site. Each step names itself on failure, and the
# first failure ends the run with a non-zero status.
fail() {
  die "check failed: $1: $2"
}

check_installed() {
  wp core is-installed || fail installed "wp core is-installed reports no installation"
}

# The site URL names a host port that is not reachable from inside the guest,
# so ask the local web server for it with the configured Host header.
check_http() {
  local host=${url#http://} code admin location
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $host" "http://$http_address/") || true
  [ "$code" = 200 ] || fail front-page "GET / returned '$code', expected 200"
  admin=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' -H "Host: $host" "http://$http_address/wp-admin/" || true)
  code=${admin%% *}
  location=${admin#* }
  [ "$code" = 302 ] && [[ "$location" == */wp-login.php* ]] \
    || fail admin "GET /wp-admin/ returned '$code' to '$location', expected a redirect to wp-login.php"
}

# WordPress requesting its own URL, as WP-Cron, Site Health and static
# exporters do. The guest serves the URL's port and host for this.
check_loopback() {
  local code
  # shellcheck disable=SC2016 # PHP code, expanded by wp eval
  code=$(wp eval '$r = wp_remote_get( home_url( "/" ), array( "timeout" => 10 ) ); echo is_wp_error( $r ) ? $r->get_error_message() : wp_remote_retrieve_response_code( $r );') || true
  [ "$code" = 200 ] || fail loopback "WordPress requesting $url/ got '$code', expected 200"
}

# Send a message through WordPress and find it in Mailpit's API.
check_mail() {
  local api="http://127.0.0.1:$mailpit_ui_port/api/v1" subject id='' tries=0
  curl -fs "$api/info" >/dev/null || fail mailpit "API not reachable on port $mailpit_ui_port"
  subject="wp-site check $(date +%s%N)"
  CHECK_SUBJECT=$subject wp eval 'exit( wp_mail( "check@example.test", getenv( "CHECK_SUBJECT" ), "wp-site check" ) ? 0 : 1 );' \
    || fail mail "wp_mail returned false"
  while [ -z "$id" ] && [ "$tries" -lt 10 ]; do
    id=$(curl -fs "$api/messages" | jq -r --arg subject "$subject" '.messages[] | select(.Subject == $subject) | .ID' | head -n1)
    tries=$((tries + 1))
    [ -n "$id" ] || sleep 1
  done
  [ -n "$id" ] || fail mail "message '$subject' did not reach Mailpit"
  curl -fs -X DELETE -H 'Content-Type: application/json' -d "{\"IDs\":[\"$id\"]}" "$api/messages" >/dev/null
}

check() {
  load_settings
  local step
  for step in installed http loopback mail; do
    "check_$step"
    echo "ok: $step"
  done
}

case "${1:-}" in
  setup) setup ;;
  check) check ;;
  *) die "usage: wp-site setup|check" ;;
esac

# wp-site: setup and check logic for the nixant-wp WordPress site.
#
# Everything site-specific comes from the JSON file named by WP_SITE_SETTINGS,
# which the NixOS module generates. `setup` is idempotent: it is rerun by
# wordpress-setup.service whenever the settings change and must converge
# without touching wp-config.php, wp-content or the database more than needed.

die() {
  echo "wp-site: $*" >&2
  exit 1
}

setting() {
  jq -er "$1" "$WP_SITE_SETTINGS"
}

load_settings() {
  : "${WP_SITE_SETTINGS:?WP_SITE_SETTINGS must name the generated settings file}"
  root=$(setting .root)
  core=$(setting .core)
  core_id=$(setting .coreId)
  url=$(setting .url)
  title=$(setting .title)
  admin_user=$(setting .admin.user)
  admin_password=$(setting .admin.password)
  admin_email=$(setting .admin.email)
  workspace=$(setting .workspace)
  db_name=$(setting .db.name)
  db_user=$(setting .db.user)
  db_socket=$(setting .db.socket)
  mailpit_url=$(setting '.mailpitUrl // "not forwarded to the host"')
  # The bundled wrapper has no useful HOME in a system service, and its cache
  # must not land in the web root unprotected: Caddy hides dotfiles.
  export WP_CLI_CACHE_DIR="$root/.cache/wp-cli"
}

wp() {
  command wp --path="$root" "$@"
}

# Store files are read-only; copy them as plain writable files.
copy_core() {
  local installed=
  [ -e "$root/.core-version" ] && installed=$(<"$root/.core-version")
  if [ "$installed" = "$core_id" ]; then
    return
  fi
  [ -e "$core/wp-includes/version.php" ] || die "$core is not a WordPress core (no wp-includes/version.php)"
  echo "wp-site: installing WordPress core ($core_id)"
  mkdir -p "$root"
  rsync -rltp --chmod=Du=rwx,Dg=rx,Fu=rw,Fg=r --delete \
    --exclude=/wp-config.php --exclude=/wp-content/ \
    --exclude=/.core-version --exclude=/.cache/ \
    "$core"/ "$root"/
  # Bundled themes and plugins are seeded once and only added afterwards, as
  # WordPress's own updater does; content the user changed is left alone.
  mkdir -p "$root/wp-content"
  rsync -rltp --chmod=Du=rwx,Dg=rx,Fu=rw,Fg=r --ignore-existing \
    "$core/wp-content/" "$root/wp-content/"
  chmod 0750 "$root"
  if [ -e "$root/wp-config.php" ] && wp core is-installed; then
    wp core update-db
  fi
  printf '%s\n' "$core_id" >"$root/.core-version"
}

configure() {
  if [ ! -e "$root/wp-config.php" ]; then
    wp config create --skip-check --dbname="$db_name" --dbuser="$db_user" \
      --dbpass='' --dbhost="localhost:$db_socket"
  fi
  wp config set DB_NAME "$db_name"
  wp config set DB_USER "$db_user"
  wp config set DB_PASSWORD ''
  wp config set DB_HOST "localhost:$db_socket"
  # Core is managed by Nix; WordPress must not replace it behind its back.
  wp config set AUTOMATIC_UPDATER_DISABLED true --raw
  wp config set WP_AUTO_UPDATE_CORE false --raw
  # Booleans and integers are written as PHP literals, strings as strings.
  local key kind value
  while IFS=$'\t' read -r key kind value; do
    case "$kind" in
      boolean | number) wp config set "$key" "$value" --raw ;;
      *) wp config set "$key" "$value" ;;
    esac
  done < <(jq -r '.wpConfig | to_entries[] | [.key, (.value | type), (.value | tostring)] | @tsv' "$WP_SITE_SETTINGS")
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
}

# Keep home, siteurl and the permalink structure at their configured values,
# for example after the forwarded host port changed.
converge_urls() {
  local option
  for option in home siteurl; do
    if [ "$(wp option get "$option")" != "$url" ]; then
      wp option update "$option" "$url"
    fi
  done
  if [ "$(wp option get permalink_structure)" != '/%postname%/' ]; then
    wp rewrite structure '/%postname%/'
  fi
}

# Symlink every declared plugin or theme into wp-content, so host edits in the
# workspace are live. Links this module created earlier (they point into the
# workspace) but that are no longer declared are removed.
link_components() {
  local kind=$1 dir="$root/wp-content/$1" slug path source target link
  mkdir -p "$dir"
  while IFS=$'\t' read -r slug path; do
    source="$workspace/$path"
    target="$dir/$slug"
    [ -e "$source" ] || die "$kind $slug: $source does not exist in the workspace"
    if [ -L "$target" ]; then
      ln -sfn "$source" "$target"
    elif [ -e "$target" ]; then
      die "$kind $slug: $target exists and is not a symlink; remove it or rename the $kind"
    else
      ln -s "$source" "$target"
    fi
  done < <(jq -r --arg kind "$kind" '.[$kind] | to_entries[] | [.key, .value.path] | @tsv' "$WP_SITE_SETTINGS")

  for link in "$dir"/*; do
    [ -L "$link" ] || continue
    case "$(readlink "$link")" in
      "$workspace"/*) ;;
      *) continue ;;
    esac
    slug=${link##*/}
    if ! jq -e --arg kind "$kind" --arg slug "$slug" '.[$kind] | has($slug)' "$WP_SITE_SETTINGS" >/dev/null; then
      echo "wp-site: removing $kind $slug, no longer declared"
      if [ "$kind" = plugins ]; then
        wp plugin deactivate "$slug"
      fi
      rm "$link"
    fi
  done
}

activate_components() {
  local slug theme
  while read -r slug; do
    wp plugin activate "$slug"
  done < <(jq -r '.plugins | to_entries[] | select(.value.activate) | .key' "$WP_SITE_SETTINGS")
  theme=$(jq -r '.activeTheme // empty' "$WP_SITE_SETTINGS")
  if [ -n "$theme" ]; then
    # Linked and bundled themes are installed already; anything else comes
    # from wordpress.org, the one step that needs network access.
    if ! wp theme is-installed "$theme"; then
      wp theme install "$theme"
    fi
    wp theme activate "$theme"
  fi
}

report() {
  cat <<REPORT
WordPress is ready:
  site:    $url
  admin:   $url/wp-admin/  ($admin_user / $admin_password; development only)
  mailpit: $mailpit_url
REPORT
}

setup() {
  load_settings
  set -o errtrace # so the trap also covers the functions below
  trap 'echo "wp-site: setup failed at: $BASH_COMMAND" >&2' ERR
  copy_core
  configure
  wait_for_database
  install_site
  converge_urls
  link_components plugins
  link_components themes
  activate_components
  report
}

case "${1:-}" in
  setup) setup ;;
  check) die "check is not implemented yet" ;;
  *) die "usage: wp-site setup|check" ;;
esac

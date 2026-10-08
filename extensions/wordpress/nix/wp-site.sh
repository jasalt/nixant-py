# wp-site: setup and check logic for the nixant-wp WordPress site.
# Stub: `setup` and `check` are implemented by later tasks.
usage() {
  echo "usage: wp-site setup|check" >&2
  exit 2
}

case "${1:-}" in
  setup | check)
    echo "wp-site $1: not implemented yet" >&2
    exit 1
    ;;
  *) usage ;;
esac

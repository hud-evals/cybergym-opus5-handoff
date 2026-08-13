#!/bin/sh
# Unmasked private upstream server used by this fidelity profile.
set -eu
set +x
umask 077
ulimit -c 0 2>/dev/null || true

case "${1-}" in
    -h|--help)
        printf '%s\n' 'Usage: server.sh  # configuration is read from /etc/cybergym/server.env by systemd'
        exit 0
        ;;
esac

: "${CG_REPOSITORY_ROOT:?CG_REPOSITORY_ROOT is required}"
: "${CG_RESULTS_DIR:?CG_RESULTS_DIR is required}"
: "${CG_SERVER_MODE:?CG_SERVER_MODE is required}"
: "${CG_UV_BIN:?CG_UV_BIN is required}"
: "${CYBERGYM_API_KEY:?CYBERGYM_API_KEY is required; public default is forbidden}"

PORT=${CG_SERVER_PORT:-8666}
HOST=$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}')
[ -n "$HOST" ] || { printf '%s\n' 'server: Docker bridge has no gateway' >&2; exit 1; }

SERVER_DIR=$CG_RESULTS_DIR/server
mkdir -p "$SERVER_DIR"
set -- \
    --host "$HOST" \
    --port "$PORT" \
    --log_dir "$SERVER_DIR" \
    --db_path "$SERVER_DIR/poc.db"

case "$CG_SERVER_MODE" in
    images) ;;
    binary)
        : "${CG_SERVER_BINARY_DIR:?CG_SERVER_BINARY_DIR is required in binary mode}"
        set -- "$@" --binary_dir "$CG_SERVER_BINARY_DIR"
        ;;
    *) printf 'server: unsupported CG_SERVER_MODE: %s\n' "$CG_SERVER_MODE" >&2; exit 1 ;;
esac

# Deliberately no --mask_map_path: the pinned OpenHands example submits the
# real task ID. Binding only to the Docker bridge keeps private routes off LAN
# and Tailscale interfaces while remaining reachable from agent containers.
exec "$CG_UV_BIN" run --frozen --no-sync --project "$CG_REPOSITORY_ROOT/integrations/hud" \
    python -m cybergym.server "$@"

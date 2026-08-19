#!/bin/sh
# Run the task-scoped HTTPS relay's private HTTP backend.
set -eu
set +x
umask 077
ulimit -c 0 2>/dev/null || true

: "${CG_REPOSITORY_ROOT:?CG_REPOSITORY_ROOT is required}"
: "${CG_SERVER_URL:?CG_SERVER_URL is required}"
: "${CG_UV_BIN:?CG_UV_BIN is required}"
: "${CG_DAYTONA_RELAY_ADMIN_TOKEN:?CG_DAYTONA_RELAY_ADMIN_TOKEN is required}"
: "${CG_DAYTONA_RELAY_REGISTRY:?CG_DAYTONA_RELAY_REGISTRY is required}"

exec "$CG_UV_BIN" run --frozen --no-sync --project "$CG_REPOSITORY_ROOT/integrations/hud" \
    cybergym-hud-daytona-relay \
    --registry "$CG_DAYTONA_RELAY_REGISTRY" \
    --upstream "$CG_SERVER_URL" \
    --host 127.0.0.1 \
    --port 18765 \
    --enable-admin

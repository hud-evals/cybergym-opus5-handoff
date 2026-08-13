#!/bin/sh
# Validate the complete paid catalog and requested worker width without inference.
set -eu
set +x
ulimit -c 0 2>/dev/null || true

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$DEFAULT_ROOT}
DATA_DIR=${CG_DATA_DIR:-}
SOURCE_PROVENANCE=${CG_DATA_PROVENANCE:-}
RESULTS_DIR=${CG_RESULTS_DIR:-}
SERVER_MODE=${CG_SERVER_MODE:-images}
SERVER_URL=${CG_SERVER_URL:-}
SERVER_BINARY_DIR=${CG_SERVER_BINARY_DIR:-}
SERVER_DEPLOYMENT_SEAL=${CG_SERVER_DEPLOYMENT_SEAL:-}
MAX_CONCURRENT=${CG_CAMPAIGN_MAX_CONCURRENT:-1}
UV_BIN=${CG_UV_BIN:-uv}

usage() {
    cat <<'EOF'
Usage: campaign-preflight.sh [--max-concurrent 1..6] [--repository-root PATH]

Run common authentication/fidelity checks plus every catalog data/grader and
capacity check. This command makes no model/inference call.
EOF
}

die() {
    printf 'campaign-preflight: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --max-concurrent)
            [ "$#" -ge 2 ] || die "--max-concurrent requires a value"
            MAX_CONCURRENT=$2
            shift 2
            ;;
        --repository-root)
            [ "$#" -ge 2 ] || die "--repository-root requires a path"
            REPOSITORY_ROOT=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *) die "unknown option: $1" ;;
    esac
done

[ -n "$DATA_DIR" ] || die "CG_DATA_DIR is required"
[ -n "$SOURCE_PROVENANCE" ] || die "CG_DATA_PROVENANCE is required"
[ -n "$RESULTS_DIR" ] || die "CG_RESULTS_DIR is required"
[ -n "$SERVER_URL" ] || die "CG_SERVER_URL is required"
case "$MAX_CONCURRENT" in
    1|2|3|4|5|6) ;;
    *) die "--max-concurrent must be between 1 and 6" ;;
esac

mkdir -p "$RESULTS_DIR/campaign-gpt56-sol-200"

# The common preflight validates host/source/runtime identities, HUD and model
# access without inference, server auth, and one concrete task end to end.
CG_REPOSITORY_ROOT=$REPOSITORY_ROOT "$SCRIPT_DIR/preflight.sh"

set -- "$UV_BIN" run --frozen --no-sync --project "$REPOSITORY_ROOT/integrations/hud" cybergym-hud-preflight-catalog \
    --repository-root "$REPOSITORY_ROOT" \
    --data-dir "$DATA_DIR" \
    --source-provenance "$SOURCE_PROVENANCE" \
    --server "$SERVER_URL" \
    --server-mode "$SERVER_MODE" \
    --max-concurrent "$MAX_CONCURRENT" \
    --report "$RESULTS_DIR/campaign-gpt56-sol-200/full-corpus-preflight.json"
if [ "$SERVER_MODE" = binary ]; then
    [ -n "$SERVER_BINARY_DIR" ] || die "binary mode requires CG_SERVER_BINARY_DIR"
    [ -n "$SERVER_DEPLOYMENT_SEAL" ] || die "binary mode requires CG_SERVER_DEPLOYMENT_SEAL"
    set -- "$@" --server-binary-dir "$SERVER_BINARY_DIR" \
        --server-deployment-seal "$SERVER_DEPLOYMENT_SEAL"
fi
exec "$@"

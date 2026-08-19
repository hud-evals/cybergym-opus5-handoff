#!/bin/sh
# Spend-gated, restart-safe full-catalog CyberGym campaign.
set -eu
set +x
umask 077
ulimit -c 0 2>/dev/null || true

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$DEFAULT_ROOT}
DATA_DIR=${CG_DATA_DIR:-}
SERVER_URL=${CG_SERVER_URL:-}
RESULTS_DIR=${CG_RESULTS_DIR:-}
SERVER_MODE=${CG_SERVER_MODE:-images}
MODEL=${CG_MODEL:-claude-opus-5}
REASONING_EFFORT=${CG_REASONING_EFFORT-}
JOB_NAME=${CG_JOB_NAME:-cybergym-claude-opus-5-no-internet-v1}
RUNTIME_NETWORK=${CG_RUNTIME_NETWORK:-}
MAX_CONCURRENT=${CG_CAMPAIGN_MAX_CONCURRENT:-1}
SHARD_SIZE=${CG_CAMPAIGN_SHARD_SIZE:-12}
UV_BIN=${CG_UV_BIN:-uv}
CONFIRM_PAID_ALL=0
CONTINUE_AFTER_ERRORS=0

usage() {
    cat <<'EOF'
Usage: campaign.sh --confirm-paid-all [options]

Options:
  --max-concurrent 1..6     Rolling native rollout cap (default: 1)
  --shard-size 1..24        Deterministic HUD Job checkpoint size (default: 12)
  --continue-after-errors   Skip already-paid error traces after operator review
  --repository-root PATH

The run profile is fixed to direct claude-opus-5, 200 iterations, 3600
seconds, and HUD Job name cybergym-claude-opus-5-no-internet-v1.
EOF
}

die() {
    printf 'campaign: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm-paid-all) CONFIRM_PAID_ALL=1; shift ;;
        --continue-after-errors) CONTINUE_AFTER_ERRORS=1; shift ;;
        --max-concurrent)
            [ "$#" -ge 2 ] || die "--max-concurrent requires a value"
            MAX_CONCURRENT=$2
            shift 2
            ;;
        --shard-size)
            [ "$#" -ge 2 ] || die "--shard-size requires a value"
            SHARD_SIZE=$2
            shift 2
            ;;
        --repository-root)
            [ "$#" -ge 2 ] || die "--repository-root requires a path"
            REPOSITORY_ROOT=$2
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[ "$CONFIRM_PAID_ALL" -eq 1 ] \
    || die "refusing the complete paid catalog without explicit --confirm-paid-all"
[ -n "$DATA_DIR" ] || die "CG_DATA_DIR is required"
[ -n "$SERVER_URL" ] || die "CG_SERVER_URL is required"
[ -n "$RESULTS_DIR" ] || die "CG_RESULTS_DIR is required"
[ "$MODEL" = claude-opus-5 ] || die "paid campaign requires CG_MODEL=claude-opus-5"
[ -z "$REASONING_EFFORT" ] || die "paid campaign requires empty CG_REASONING_EFFORT"
[ "$JOB_NAME" = cybergym-claude-opus-5-no-internet-v1 ] \
    || die "paid campaign requires CG_JOB_NAME=cybergym-claude-opus-5-no-internet-v1"
[ "$RUNTIME_NETWORK" = cybergym-no-internet ] \
    || die "paid campaign requires CG_RUNTIME_NETWORK=cybergym-no-internet"
[ -z "${CG_MODEL_BASE_URL:-}" ] || die "paid campaign requires direct Anthropic (empty CG_MODEL_BASE_URL)"
case "$MAX_CONCURRENT" in
    1|2|3|4|5|6) ;;
    *) die "--max-concurrent must be between 1 and 6" ;;
esac
case "$SHARD_SIZE" in
    ''|*[!0-9]*) die "--shard-size must be an integer between 1 and 24" ;;
esac
[ "$SHARD_SIZE" -ge 1 ] && [ "$SHARD_SIZE" -le 24 ] \
    || die "--shard-size must be between 1 and 24"

# This completes before the Python campaign operator can create a HUD Job or
# call the provider. CG_MODEL_BASE_URL and every credential stay in env only.
"$SCRIPT_DIR/campaign-preflight.sh" \
    --repository-root "$REPOSITORY_ROOT" \
    --max-concurrent "$MAX_CONCURRENT"

set -- "$UV_BIN" run --frozen --no-sync --project "$REPOSITORY_ROOT/integrations/hud" cybergym-hud-run-campaign \
    --all --confirm-paid-all \
    --repository-root "$REPOSITORY_ROOT" \
    --data-dir "$DATA_DIR" \
    --server "$SERVER_URL" \
    --results-dir "$RESULTS_DIR" \
    --grader-server-mode "$SERVER_MODE" \
    --max-concurrent "$MAX_CONCURRENT" \
    --shard-size "$SHARD_SIZE"
if [ "$CONTINUE_AFTER_ERRORS" -eq 1 ]; then
    set -- "$@" --continue-after-errors
fi
exec "$@"

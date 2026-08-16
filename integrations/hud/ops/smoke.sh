#!/bin/sh
# Spend-gated one-task CyberGym smoke run.
set -eu
set +x
ulimit -c 0 2>/dev/null || true

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$DEFAULT_ROOT}
TASK_ID=${CG_SMOKE_TASK_ID:-arvo:10400}
CONFIRM_SPEND=0

usage() {
    cat <<'EOF'
Usage: smoke.sh --confirm-spend [--task-id TASK_ID] [--repository-root PATH]

Run exactly one 10-iteration CyberGym task after the no-spend preflight.
Required configuration comes from the mode-600 operator environment file.
EOF
}

die() {
    printf 'smoke: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm-spend)
            CONFIRM_SPEND=1
            shift
            ;;
        --task-id)
            [ "$#" -ge 2 ] || die "--task-id requires a value"
            TASK_ID=$2
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

[ "$CONFIRM_SPEND" -eq 1 ] \
    || die "refusing to make a model call without explicit --confirm-spend"

CG_REPOSITORY_ROOT=$REPOSITORY_ROOT CG_SMOKE_TASK_ID=$TASK_ID \
    "$SCRIPT_DIR/preflight.sh"

DATA_DIR=${CG_DATA_DIR:?CG_DATA_DIR is required}
SERVER_URL=${CG_SERVER_URL:?CG_SERVER_URL is required}
MODEL=${CG_MODEL:-gpt-5.6-sol}
REASONING_EFFORT=${CG_REASONING_EFFORT:-xhigh}
JOB_NAME=${CG_JOB_NAME:-cybergym-gpt5.6-sol-no-internet-v1}
RUNTIME_NETWORK=${CG_RUNTIME_NETWORK:?CG_RUNTIME_NETWORK is required}
RESULTS_DIR=${CG_RESULTS_DIR:?CG_RESULTS_DIR is required}
SERVER_MODE=${CG_SERVER_MODE:-images}
UV_BIN=${CG_UV_BIN:-uv}

mkdir -p "$RESULTS_DIR/logs" "$RESULTS_DIR/tmp"

if [ -n "${CG_MODEL_BASE_URL:-}" ]; then
    exec "$UV_BIN" run --frozen --no-sync --project "$REPOSITORY_ROOT/integrations/hud" \
        cybergym-hud-run-native \
        "$TASK_ID" \
        --repository-root "$REPOSITORY_ROOT" \
        --data-dir "$DATA_DIR" \
        --server "$SERVER_URL" \
        --model "$MODEL" \
        --reasoning-effort "$REASONING_EFFORT" \
        --job-name "$JOB_NAME" \
        --base-url "$CG_MODEL_BASE_URL" \
        --grader-server-mode "$SERVER_MODE" \
        --log-dir "$RESULTS_DIR/logs" \
        --tmp-dir "$RESULTS_DIR/tmp" \
        --max-iter 10 \
        --timeout 1200 \
        --max-concurrent 1 \
        --runtime-network "$RUNTIME_NETWORK"
fi

exec "$UV_BIN" run --frozen --no-sync --project "$REPOSITORY_ROOT/integrations/hud" \
    cybergym-hud-run-native \
    "$TASK_ID" \
    --repository-root "$REPOSITORY_ROOT" \
    --data-dir "$DATA_DIR" \
    --server "$SERVER_URL" \
    --model "$MODEL" \
    --reasoning-effort "$REASONING_EFFORT" \
    --job-name "$JOB_NAME" \
    --grader-server-mode "$SERVER_MODE" \
    --log-dir "$RESULTS_DIR/logs" \
    --tmp-dir "$RESULTS_DIR/tmp" \
    --max-iter 10 \
    --timeout 1200 \
    --max-concurrent 1 \
    --runtime-network "$RUNTIME_NETWORK"

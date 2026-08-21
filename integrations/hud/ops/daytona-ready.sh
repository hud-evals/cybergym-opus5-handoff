#!/bin/sh
# Prepare the existing protected CyberGym host for distributed Opus 5 lanes.
# This runs catalog/provider/placement gates but makes no model call.
set -eu
set +x
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$DEFAULT_ROOT}
RESULTS_DIR=${CG_RESULTS_DIR:-}
UV_BIN=${CG_UV_BIN:-uv}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    printf '%s\n' 'Usage: daytona-ready.sh'
    exit 0
fi
[ "$#" -eq 0 ] || {
    printf '%s\n' 'daytona-ready: this command takes no arguments' >&2
    exit 2
}
[ -n "$RESULTS_DIR" ] || { printf '%s\n' 'daytona-ready: CG_RESULTS_DIR is required' >&2; exit 1; }
[ -n "${CG_DAYTONA_RELAY_URL-}" ] \
    || { printf '%s\n' 'daytona-ready: install the task-scoped HTTPS relay first' >&2; exit 1; }
[ -n "${CG_DAYTONA_RELAY_CIDRS-}" ] \
    || { printf '%s\n' 'daytona-ready: relay public IPv4 binding is missing' >&2; exit 1; }

root=$RESULTS_DIR/daytona-anthropic
task_file=$root/full-catalog.txt
plan_dir=$root/lanes-24x8
artifact_report=$RESULTS_DIR/campaign-claude-opus-5-200-no-internet-v1/full-corpus-preflight.json
daytona_report=$root/preflight.json
run_config=${XDG_CONFIG_HOME:-$HOME/.config}/cybergym/daytona-run.env

"$UV_BIN" run --frozen --no-sync --project "$REPOSITORY_ROOT/integrations/hud" \
    python "$SCRIPT_DIR/prepare-daytona-catalog.py" \
    --repository-root "$REPOSITORY_ROOT" --output "$task_file"

if [ ! -f "$artifact_report" ]; then
    "$SCRIPT_DIR/campaign-preflight.sh" --max-concurrent 1
fi

"$UV_BIN" run --frozen --no-sync --project "$REPOSITORY_ROOT/integrations/hud" \
    python "$SCRIPT_DIR/plan-daytona-lanes.py" \
    --task-file "$task_file" --output-dir "$plan_dir" \
    --lanes 24 --max-concurrent 8 >/dev/null

"$SCRIPT_DIR/daytona-preflight.sh"

install -d -m 0700 "$(dirname "$run_config")"
temporary=$run_config.tmp.$$
trap 'rm -f "$temporary"' EXIT HUP INT TERM
cat >"$temporary" <<EOF
CG_DAYTONA_TASK_FILE=$task_file
CG_ARTIFACT_PREFLIGHT_REPORT=$artifact_report
CG_DAYTONA_PREFLIGHT_REPORT=$daytona_report
CG_DAYTONA_PLAN_DIR=$plan_dir
CG_RESULTS_DIR=$RESULTS_DIR/opus5-pass3
CG_DAYTONA_RELAY_URL=$CG_DAYTONA_RELAY_URL
CG_DAYTONA_RELAY_CIDRS=$CG_DAYTONA_RELAY_CIDRS
CG_DAYTONA_MAX_CONCURRENT=8
CG_DAYTONA_SHARD_SIZE=8
EOF
chmod 0600 "$temporary"
mv -f "$temporary" "$run_config"
trap - EXIT HUP INT TERM
printf 'CyberGym Daytona Opus 5 pass@3 is ready: 3 repeats x 24 lanes x width 8; config=%s\n' "$run_config"

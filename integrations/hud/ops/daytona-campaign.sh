#!/bin/sh
# Restart-safe selected-task Daytona campaign pinned to direct Claude Opus 5.
set -eu
set +x
umask 077
ulimit -n 65536

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$DEFAULT_ROOT}
DATA_DIR=${CG_DATA_DIR:-}
SERVER_URL=${CG_SERVER_URL:-}
RESULTS_DIR=${CG_RESULTS_DIR:-}
TASK_FILE=${CG_DAYTONA_TASK_FILE:-}
ARTIFACT_REPORT=${CG_ARTIFACT_PREFLIGHT_REPORT:-}
DAYTONA_REPORT=${CG_DAYTONA_PREFLIGHT_REPORT:-$RESULTS_DIR/daytona-anthropic/preflight.json}
KNOWN_HOSTS=${CG_DAYTONA_KNOWN_HOSTS:-$REPOSITORY_ROOT/integrations/hud/daytona_known_hosts.txt}
MAX_CONCURRENT=${CG_DAYTONA_MAX_CONCURRENT:-60}
SHARD_SIZE=${CG_DAYTONA_SHARD_SIZE:-60}
UV_BIN=${CG_UV_BIN:-uv}
CONFIRM=0

usage() {
  cat <<'EOF'
Usage: daytona-campaign.sh --confirm-paid-selection

Requires CG_DAYTONA_TASK_FILE, CG_ARTIFACT_PREFLIGHT_REPORT, the protected
operator environment files, and a passing `nix run .#daytona-preflight`.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --confirm-paid-selection) CONFIRM=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'daytona-campaign: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[ "$CONFIRM" -eq 1 ] || { printf '%s\n' 'daytona-campaign: explicit paid selection confirmation is required' >&2; exit 1; }
for value in "$DATA_DIR" "$SERVER_URL" "$RESULTS_DIR" "$TASK_FILE" "$ARTIFACT_REPORT"; do
  [ -n "$value" ] || { printf '%s\n' 'daytona-campaign: required CG_* path or URL is missing' >&2; exit 1; }
done
[ "${CG_MODEL:-claude-opus-5}" = claude-opus-5 ] || { printf '%s\n' 'daytona-campaign: model arm drifted' >&2; exit 1; }
[ -z "${CG_REASONING_EFFORT-}" ] || { printf '%s\n' 'daytona-campaign: reasoning arm drifted' >&2; exit 1; }

exec "$UV_BIN" run --frozen --no-sync --project "$REPOSITORY_ROOT/integrations/hud" \
  cybergym-hud-campaign-daytona \
  --confirm-paid-selection \
  --continue-after-errors \
  --independent-selection \
  --job-name cybergym-opus5-cyber \
  --repository-root "$REPOSITORY_ROOT" \
  --data-dir "$DATA_DIR" \
  --server "$SERVER_URL" \
  --results-dir "$RESULTS_DIR/daytona-anthropic/results" \
  --state-dir "$RESULTS_DIR/daytona-anthropic/state" \
  --task-file "$TASK_FILE" \
  --artifact-preflight-report "$ARTIFACT_REPORT" \
  --artifact-preflight-concurrency "${CG_CAMPAIGN_MAX_CONCURRENT:-1}" \
  --daytona-preflight-report "$DAYTONA_REPORT" \
  --daytona-known-hosts "$KNOWN_HOSTS" \
  --max-concurrent "$MAX_CONCURRENT" \
  --shard-size "$SHARD_SIZE"

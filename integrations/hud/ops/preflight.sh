#!/bin/sh
# No-spend operational checks for one concrete CyberGym smoke task.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$DEFAULT_ROOT}
TASK_ID=${CG_SMOKE_TASK_ID:-arvo:10400}
DATA_DIR=${CG_DATA_DIR:-}
SERVER_URL=${CG_SERVER_URL:-}
MODEL=${CG_MODEL:-claude-sonnet-4-5}
RESULTS_DIR=${CG_RESULTS_DIR:-}
RUNTIME_IMAGE=docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik

usage() {
    cat <<'EOF'
Usage: preflight.sh [options]

Validate a concrete native CyberGym smoke task without making a model call.
Values default to CG_* variables from the operator environment.

Options:
  --repository-root PATH
  --task-id TASK_ID
  --data-dir PATH
  --server URL
  --model MODEL
  --results-dir PATH
  -h, --help
EOF
}

die() {
    printf 'preflight: %s\n' "$*" >&2
    exit 1
}

ok() {
    printf 'ok: %s\n' "$1"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command is missing: $1"
}

require_secret() {
    eval "secret_value=\${$1-}"
    [ -n "$secret_value" ] || die "$1 is not set"
    unset secret_value
    ok "$1 is set (value suppressed)"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --repository-root)
            [ "$#" -ge 2 ] || die "--repository-root requires a path"
            REPOSITORY_ROOT=$2
            shift 2
            ;;
        --task-id)
            [ "$#" -ge 2 ] || die "--task-id requires a value"
            TASK_ID=$2
            shift 2
            ;;
        --data-dir)
            [ "$#" -ge 2 ] || die "--data-dir requires a path"
            DATA_DIR=$2
            shift 2
            ;;
        --server)
            [ "$#" -ge 2 ] || die "--server requires a URL"
            SERVER_URL=$2
            shift 2
            ;;
        --model)
            [ "$#" -ge 2 ] || die "--model requires a value"
            MODEL=$2
            shift 2
            ;;
        --results-dir)
            [ "$#" -ge 2 ] || die "--results-dir requires a path"
            RESULTS_DIR=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *) die "unknown option: $1" ;;
    esac
done

[ -n "$DATA_DIR" ] || die "set CG_DATA_DIR or pass --data-dir"
[ -n "$SERVER_URL" ] || die "set CG_SERVER_URL or pass --server"
[ -n "$RESULTS_DIR" ] || die "set CG_RESULTS_DIR or pass --results-dir"

case "$(uname -s)/$(uname -m)" in
    Linux/x86_64|Linux/amd64) ok 'native Linux amd64 host' ;;
    *) die "faithful runs require a native Linux amd64/x86_64 host" ;;
esac

for command_name in git docker uv poetry curl; do
    require_command "$command_name"
done

DOCKER_PLATFORM=$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null) \
    || die "Docker daemon is unavailable"
case "$DOCKER_PLATFORM" in
    linux/amd64|linux/x86_64) ok "Docker server $DOCKER_PLATFORM" ;;
    *) die "faithful runs require a Linux amd64 Docker server; found $DOCKER_PLATFORM" ;;
esac

[ -d "$REPOSITORY_ROOT/src/cybergym" ] || die "not a CyberGym checkout: $REPOSITORY_ROOT"
uv run --project "$REPOSITORY_ROOT/integrations/hud" cybergym-hud-verify \
    --repository-root "$REPOSITORY_ROOT" >/dev/null
ok 'fidelity/source contract'

OPENHANDS_ROOT=$REPOSITORY_ROOT/examples/agents/openhands/openhands-repo
[ -f "$OPENHANDS_ROOT/frontend/build/index.html" ] || die "pinned OpenHands frontend is not built; run ops/setup.sh"
(cd "$OPENHANDS_ROOT" && poetry run python -c 'import openhands' >/dev/null 2>&1) \
    || die "pinned OpenHands Python environment is not built; run ops/setup.sh"
ok 'pinned OpenHands build'

docker image inspect "$RUNTIME_IMAGE" >/dev/null 2>&1 \
    || die "missing OpenHands runtime image: $RUNTIME_IMAGE"
ok 'pinned OpenHands runtime image'

case "$TASK_ID" in
    arvo:*)
        SUBSET=arvo
        SUBID=${TASK_ID#arvo:}
        VUL_IMAGE=n132/arvo:$SUBID-vul
        FIX_IMAGE=n132/arvo:$SUBID-fix
        ;;
    oss-fuzz:*)
        SUBSET=oss-fuzz
        SUBID=${TASK_ID#oss-fuzz:}
        VUL_IMAGE=cybergym/oss-fuzz:$SUBID-vul
        FIX_IMAGE=cybergym/oss-fuzz:$SUBID-fix
        ;;
    *) die "smoke task must be arvo:* or oss-fuzz:*" ;;
esac

case "$SUBID" in
    ''|*[!0-9]*) die "task ID suffix must be numeric: $TASK_ID" ;;
esac

TASK_DATA=$DATA_DIR/$SUBSET/$SUBID
[ -f "$TASK_DATA/description.txt" ] || die "missing task description: $TASK_DATA/description.txt"
[ -f "$TASK_DATA/repo-vul.tar.gz" ] || die "missing vulnerable source archive: $TASK_DATA/repo-vul.tar.gz"
ok "task data for $TASK_ID"

docker image inspect "$VUL_IMAGE" >/dev/null 2>&1 || die "missing target image: $VUL_IMAGE"
docker image inspect "$FIX_IMAGE" >/dev/null 2>&1 || die "missing target image: $FIX_IMAGE"
ok "vulnerable and fixed images for $TASK_ID"

[ -d "$RESULTS_DIR" ] || die "results directory does not exist: $RESULTS_DIR"
[ -w "$RESULTS_DIR" ] || die "results directory is not writable: $RESULTS_DIR"
ok 'writable results directory'

require_secret HUD_API_KEY
require_secret CYBERGYM_API_KEY
case "$MODEL" in
    claude-*) require_secret ANTHROPIC_API_KEY ;;
    gpt-*|o3*|o4*) require_secret OPENAI_API_KEY ;;
    *) require_secret LLM_API_KEY ;;
esac

SERVER_URL=${SERVER_URL%/}
# Supply the private key on stdin rather than in argv or a temporary file.
{
    printf 'header = "X-API-Key: %s"\n' "$CYBERGYM_API_KEY"
    printf '%s\n' 'header = "Content-Type: application/json"'
    printf '%s\n' 'data = "{\"agent_id\":\"hud-operator-preflight\"}"'
    printf '%s\n' 'silent'
    printf '%s\n' 'show-error'
    printf '%s\n' 'fail'
} | curl --config - --connect-timeout 5 --max-time 15 "$SERVER_URL/query-poc" >/dev/null \
    || die "private CyberGym server/authentication check failed at $SERVER_URL"
ok 'private CyberGym server and key'

printf 'preflight passed for %s with model %s; no model call was made\n' "$TASK_ID" "$MODEL"

#!/bin/sh
# No-spend operational checks for one concrete CyberGym smoke task.
set -eu
set +x
ulimit -c 0 2>/dev/null || true

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$DEFAULT_ROOT}
TASK_ID=${CG_SMOKE_TASK_ID:-arvo:10400}
DATA_DIR=${CG_DATA_DIR:-}
SERVER_URL=${CG_SERVER_URL:-}
MODEL=${CG_MODEL:-claude-sonnet-4-5}
MODEL_BASE_URL=${CG_MODEL_BASE_URL:-}
RESULTS_DIR=${CG_RESULTS_DIR:-}
SERVER_MODE=${CG_SERVER_MODE:-images}
SERVER_BINARY_DIR=${CG_SERVER_BINARY_DIR:-}
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
  --server-mode images|binary
  --server-binary-dir PATH
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
    case "$1" in
        HUD_API_KEY) [ -n "${HUD_API_KEY-}" ] || die "$1 is not set" ;;
        CYBERGYM_API_KEY) [ -n "${CYBERGYM_API_KEY-}" ] || die "$1 is not set" ;;
        ANTHROPIC_API_KEY) [ -n "${ANTHROPIC_API_KEY-}" ] || die "$1 is not set" ;;
        OPENAI_API_KEY) [ -n "${OPENAI_API_KEY-}" ] || die "$1 is not set" ;;
        LLM_API_KEY) [ -n "${LLM_API_KEY-}" ] || die "$1 is not set" ;;
        *) die "unsupported secret variable: $1" ;;
    esac
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
        --server-mode)
            [ "$#" -ge 2 ] || die "--server-mode requires images or binary"
            SERVER_MODE=$2
            shift 2
            ;;
        --server-binary-dir)
            [ "$#" -ge 2 ] || die "--server-binary-dir requires a path"
            SERVER_BINARY_DIR=$2
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

for command_name in git docker uv poetry; do
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
if grep -q '^version https://git-lfs.github.com/spec/v1$' "$TASK_DATA/description.txt"; then
    die "task description is an unresolved Git LFS pointer: $TASK_DATA/description.txt"
fi
if grep -q '^version https://git-lfs.github.com/spec/v1$' "$TASK_DATA/repo-vul.tar.gz"; then
    die "vulnerable source archive is an unresolved Git LFS pointer: $TASK_DATA/repo-vul.tar.gz"
fi
tar -tzf "$TASK_DATA/repo-vul.tar.gz" >/dev/null 2>&1 \
    || die "vulnerable source archive is not a readable gzip tar: $TASK_DATA/repo-vul.tar.gz"
ok "task data for $TASK_ID"

case "$SERVER_MODE" in
    images)
        docker image inspect "$VUL_IMAGE" >/dev/null 2>&1 || die "missing target image: $VUL_IMAGE"
        docker image inspect "$FIX_IMAGE" >/dev/null 2>&1 || die "missing target image: $FIX_IMAGE"
        ok "image-mode vulnerable and fixed targets for $TASK_ID"
        ;;
    binary)
        [ -n "$SERVER_BINARY_DIR" ] || die "binary mode requires CG_SERVER_BINARY_DIR or --server-binary-dir"
        BINARY_TASK=$SERVER_BINARY_DIR/$SUBSET/$SUBID
        case "$SUBSET" in
            arvo)
                for mode in vul fix; do
                    [ -x "$BINARY_TASK/$mode/arvo" ] || die "missing executable binary target: $BINARY_TASK/$mode/arvo"
                    [ -d "$BINARY_TASK/$mode/out" ] || die "missing binary target output: $BINARY_TASK/$mode/out"
                    RUNNER_IMAGE=cybergym/oss-fuzz-base-runner:latest
                    if [ -f "$BINARY_TASK/$mode/runner" ]; then
                        IFS= read -r RUNNER_IMAGE <"$BINARY_TASK/$mode/runner"
                        [ -n "$RUNNER_IMAGE" ] || die "empty binary runner image file: $BINARY_TASK/$mode/runner"
                    fi
                    docker image inspect "$RUNNER_IMAGE" >/dev/null 2>&1 \
                        || die "missing binary-mode runner image for $mode: $RUNNER_IMAGE"
                done
                ;;
            oss-fuzz)
                for mode in vul fix; do
                    [ -f "$BINARY_TASK/$mode/metadata.json" ] \
                        || die "missing binary target metadata: $BINARY_TASK/$mode/metadata.json"
                    [ -d "$BINARY_TASK/$mode/out" ] || die "missing binary target output: $BINARY_TASK/$mode/out"
                done
                docker image inspect cybergym/oss-fuzz-base-runner:latest >/dev/null 2>&1 \
                    || die "missing upstream binary-mode runner image: cybergym/oss-fuzz-base-runner:latest"
                ;;
        esac
        ok "upstream binary-only vulnerable and fixed targets for $TASK_ID"
        ;;
    *) die "CG_SERVER_MODE must be images or binary; found: $SERVER_MODE" ;;
esac

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

# HUD telemetry is best-effort inside the SDK. Authenticate it before provider
# spend so a mistyped key cannot produce a paid rollout with no remote Job.
uv run --project "$REPOSITORY_ROOT/integrations/hud" hud models list --json >/dev/null 2>&1 \
    || die "HUD_API_KEY authentication failed"
ok 'HUD authentication (no model call)'

# For the direct OpenAI profile, authenticate and confirm the exact pinned
# model without creating a completion. Custom gateways are protocol-specific,
# so their key is presence-checked above and exercised only by the spend gate.
case "$MODEL" in
    gpt-*|o3*|o4*)
        if [ -z "$MODEL_BASE_URL" ]; then
            uv run --project "$REPOSITORY_ROOT/integrations/hud" python - "$MODEL" <<'PY' \
                || die "OPENAI_API_KEY authentication or model access failed"
import os
import sys
from urllib.parse import quote

import httpx

model = quote(sys.argv[1], safe="")
response = httpx.get(
    f"https://api.openai.com/v1/models/{model}",
    headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
    timeout=15.0,
)
raise SystemExit(0 if response.status_code == 200 else 1)
PY
            ok 'OpenAI authentication and exact model access (no inference)'
        else
            ok 'custom model endpoint configured; provider key presence checked'
        fi
        ;;
esac

SERVER_URL=${SERVER_URL%/}
# The authenticated empty-database response is intentionally HTTP 404 with
# detail "Record not found". A bad API key is also hidden behind HTTP 404, but
# has detail "Not found". Inspect both status and body without putting the key
# in argv, a temporary file, or output.
uv run --project "$REPOSITORY_ROOT/integrations/hud" python - "$SERVER_URL" <<'PY' \
    || die "private CyberGym server/authentication check failed at $SERVER_URL"
import os
import sys

import httpx

base_url = sys.argv[1]
response = httpx.post(
    f"{base_url}/query-poc",
    headers={"X-API-Key": os.environ["CYBERGYM_API_KEY"]},
    json={"agent_id": "hud-operator-preflight"},
    timeout=15.0,
)
if response.status_code == 200:
    raise SystemExit(0)
try:
    detail = response.json().get("detail")
except (ValueError, AttributeError):
    detail = None
if response.status_code == 404 and detail == "Record not found":
    raise SystemExit(0)
raise SystemExit(1)
PY
ok 'private CyberGym server and key'

printf 'preflight passed for %s with model %s; no model call was made\n' "$TASK_ID" "$MODEL"

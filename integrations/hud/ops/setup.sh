#!/bin/sh
# Prepare the pinned native CyberGym/OpenHands runner. This script makes no
# model calls and never reads provider credentials.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$DEFAULT_ROOT}
SKIP_RUNTIME_IMAGE=0
SKIP_OPENHANDS_BUILD=0

usage() {
    cat <<'EOF'
Usage: setup.sh [options]

Prepare a native Linux/amd64 CyberGym fidelity runner without making model calls.

Options:
  --repository-root PATH     CyberGym checkout (default: inferred from script)
  --skip-runtime-image       Do not pull the pinned OpenHands runtime image
  --skip-openhands-build     Do not build the pinned OpenHands checkout
  -h, --help                 Show this help
EOF
}

die() {
    printf 'setup: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command is missing: $1"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --repository-root)
            [ "$#" -ge 2 ] || die "--repository-root requires a path"
            REPOSITORY_ROOT=$2
            shift 2
            ;;
        --skip-runtime-image)
            SKIP_RUNTIME_IMAGE=1
            shift
            ;;
        --skip-openhands-build)
            SKIP_OPENHANDS_BUILD=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

case "$(uname -s)/$(uname -m)" in
    Linux/x86_64|Linux/amd64) ;;
    *) die "faithful runs require a native Linux amd64/x86_64 host" ;;
esac

for command_name in git docker uv make poetry node npm; do
    require_command "$command_name"
done

[ -d "$REPOSITORY_ROOT/src/cybergym" ] || die "not a CyberGym checkout: $REPOSITORY_ROOT"

DOCKER_PLATFORM=$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null) \
    || die "Docker daemon is unavailable"
case "$DOCKER_PLATFORM" in
    linux/amd64|linux/x86_64) ;;
    *) die "faithful runs require a Linux amd64 Docker server; found $DOCKER_PLATFORM" ;;
esac

printf '%s\n' 'Initializing the pinned agent submodule...'
git -C "$REPOSITORY_ROOT" submodule update --init --recursive examples/agents

printf '%s\n' 'Installing the HUD integration...'
uv sync --frozen --project "$REPOSITORY_ROOT/integrations/hud" --extra test

if [ "$SKIP_RUNTIME_IMAGE" -eq 0 ]; then
    "$SCRIPT_DIR/runtime-image.sh" ensure
fi

OPENHANDS_ROOT=$REPOSITORY_ROOT/examples/agents/openhands/openhands-repo
[ -d "$OPENHANDS_ROOT" ] || die "pinned OpenHands checkout is missing after submodule initialization"
if [ "$SKIP_OPENHANDS_BUILD" -eq 0 ]; then
    if [ -f "$OPENHANDS_ROOT/frontend/build/index.html" ] \
        && (cd "$OPENHANDS_ROOT" && poetry run python -c 'import openhands' >/dev/null 2>&1); then
        printf '%s\n' 'Pinned OpenHands build is already ready.'
    else
        printf '%s\n' 'Building the pinned OpenHands checkout...'
        (cd "$OPENHANDS_ROOT" && make build INSTALL_PLAYWRIGHT=false)
    fi
fi

uv run --frozen --no-sync --project "$REPOSITORY_ROOT/integrations/hud" cybergym-hud-verify \
    --repository-root "$REPOSITORY_ROOT"
printf '%s\n' 'Setup complete. Download task data/images, start the private server, then run ops/preflight.sh.'

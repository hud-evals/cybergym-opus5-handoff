#!/bin/sh
# Build a fresh Linux EC2 host from public artifacts without a model call.
set -eu
set +x
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
OPERATOR=${SUDO_USER:-$(id -un)}
WORKERS=16

usage() {
    cat <<'EOF'
Usage: nix run .#bootstrap [-- --workers 1..32]

On a fresh Ubuntu 24.04 x86_64 EC2 host, install and verify the complete
CyberGym grader, source corpus, HTTPS Daytona relay, and durable lane services.
This command prompts only for HUD, Anthropic, and Daytona API keys and makes no
model/inference request. The instance needs at least 16 vCPU, 64 GiB RAM,
500 GiB free disk, and inbound TCP 80/443 for the task-scoped relay.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --operator) [ "$#" -ge 2 ] || exit 2; OPERATOR=$2; shift 2 ;;
        --workers) [ "$#" -ge 2 ] || exit 2; WORKERS=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'bootstrap-host: unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done
case "$WORKERS" in ''|*[!0-9]*) printf '%s\n' 'bootstrap-host: --workers must be numeric' >&2; exit 2 ;; esac
[ "$WORKERS" -ge 1 ] && [ "$WORKERS" -le 32 ] \
    || { printf '%s\n' 'bootstrap-host: --workers must be between 1 and 32' >&2; exit 2; }

if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$SCRIPT_DIR/bootstrap-host.sh" --operator "$OPERATOR" --workers "$WORKERS"
fi

case "$(uname -s)/$(uname -m)" in
    Linux/x86_64|Linux/amd64) ;;
    *) printf '%s\n' 'bootstrap-host: requires native Linux x86_64' >&2; exit 1 ;;
esac
id "$OPERATOR" >/dev/null 2>&1 || { printf 'bootstrap-host: unknown operator: %s\n' "$OPERATOR" >&2; exit 1; }
[ -r /dev/tty ] && [ -w /dev/tty ] \
    || { printf '%s\n' 'bootstrap-host: use an interactive SSH session so keys can be entered privately' >&2; exit 1; }
[ -z "$(git -C "$REPOSITORY_ROOT" status --porcelain=v1 --untracked-files=all)" ] \
    || { printf '%s\n' 'bootstrap-host: checkout must be clean before it can be attested' >&2; exit 1; }
HOME_DIR=$(getent passwd "$OPERATOR" | cut -d: -f6)

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl docker.io git jq openssh-client rsync zstd
systemctl enable --now docker
usermod -aG docker "$OPERATOR"

install -d -m 0755 -o root -g root /srv/cybergym /srv/cybergym-runtime
for path in \
    /srv/cybergym/results-og-fidelity \
    /srv/cybergym/uv-cache \
    /srv/cybergym/docker-config \
    /srv/cybergym/operator-cache; do
    install -d -m 0700 -o "$OPERATOR" -g "$OPERATOR" "$path"
done

configured=0
for path in server.env runner.env daytona.env relay.env; do
    [ -f "/etc/cybergym/$path" ] && configured=$((configured + 1))
done
case "$configured" in
    0)
        "$SCRIPT_DIR/configure-secrets.sh" --operator "$OPERATOR" --repository-root "$REPOSITORY_ROOT"
        ;;
    4)
        printf '%s\n' 'Protected key configuration already exists; preserving it for restart-safe resume.'
        ;;
    *)
        printf '%s\n' 'bootstrap-host: protected key configuration is incomplete; repair /etc/cybergym first' >&2
        exit 1
        ;;
esac

run_as_operator() {
    runuser -u "$OPERATOR" -- env \
        HOME="$HOME_DIR" \
        USER="$OPERATOR" \
        PATH="$PATH" \
        UV_CACHE_DIR=/srv/cybergym/uv-cache \
        XDG_CACHE_HOME=/srv/cybergym/operator-cache \
        "$@"
}

run_as_operator "$SCRIPT_DIR/setup.sh" --repository-root "$REPOSITORY_ROOT"
python3 "$SCRIPT_DIR/install-corpus.py" --workers "$WORKERS"

for reference in \
    cybergym/oss-fuzz-base-runner:latest \
    cybergym/oss-fuzz-base-runner:20190802 \
    cybergym/oss-fuzz-base-runner:20200102 \
    cybergym/oss-fuzz-base-runner:20220102; do
    docker pull "$reference"
done

set -a
# shellcheck disable=SC1091
. /etc/cybergym/server.env
set +a
"$SCRIPT_DIR/install-service.sh" --confirm-install --operator "$OPERATOR" --repository-root "$REPOSITORY_ROOT"
"$CG_UV_BIN" run --frozen --no-sync --project "$REPOSITORY_ROOT/integrations/hud" \
    cybergym-hud-attest-grader capture \
    --repository-root "$REPOSITORY_ROOT" \
    --binary-dir "$CG_SERVER_BINARY_DIR" \
    --server-url "$CG_SERVER_URL" \
    --seal "$CG_SERVER_DEPLOYMENT_SEAL"

"$SCRIPT_DIR/install-relay.sh" \
    --confirm-public-relay \
    --operator "$OPERATOR" \
    --repository-root "$REPOSITORY_ROOT"
"$SCRIPT_DIR/install-daytona-fleet.sh" \
    --confirm-install \
    --operator "$OPERATOR" \
    --repository-root "$REPOSITORY_ROOT"

run_as_operator "$SCRIPT_DIR/cybergym-ops" daytona-ready

cat <<EOF
Fresh-host CyberGym setup passed without a model call.

Paid fleet controls:
  nix run .#daytona -- start
  nix run .#daytona -- status
  nix run .#daytona -- pause
  nix run .#daytona -- resume

Keys can be rotated later with:
  sudo -E nix run .#update-keys
EOF

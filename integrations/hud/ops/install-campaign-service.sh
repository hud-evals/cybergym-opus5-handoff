#!/bin/sh
# Install the reviewed multi-day paid campaign as a durable systemd service.
set -eu
set +x
umask 077
ulimit -c 0 2>/dev/null || true

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$DEFAULT_ROOT}
OPERATOR=${SUDO_USER:-rose}
CONFIRM=0

usage() {
    cat <<'EOF'
Usage: sudo install-campaign-service.sh --confirm-paid-all [--operator USER] [--repository-root PATH]

Installs, enables, and starts cybergym-campaign.service at rolling concurrency
4. The service's campaign helper runs the complete no-inference catalog
preflight before it can create a HUD Job or call the model.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm-paid-all) CONFIRM=1; shift ;;
        --operator) [ "$#" -ge 2 ] || exit 2; OPERATOR=$2; shift 2 ;;
        --repository-root) [ "$#" -ge 2 ] || exit 2; REPOSITORY_ROOT=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'install-campaign-service: unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

[ "$CONFIRM" -eq 1 ] \
    || { printf '%s\n' 'install-campaign-service: refusing without --confirm-paid-all' >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'install-campaign-service: run with sudo' >&2; exit 1; }
id "$OPERATOR" >/dev/null 2>&1 \
    || { printf 'install-campaign-service: unknown operator: %s\n' "$OPERATOR" >&2; exit 1; }
[ -x "$REPOSITORY_ROOT/integrations/hud/ops/cybergym-ops" ] \
    || { printf '%s\n' 'install-campaign-service: cybergym-ops is missing' >&2; exit 1; }
for path in /etc/cybergym/server.env /etc/cybergym/runner.env; do
    [ -r "$path" ] || { printf 'install-campaign-service: missing %s\n' "$path" >&2; exit 1; }
done
systemctl is-active --quiet cybergym-server.service \
    || { printf '%s\n' 'install-campaign-service: CyberGym server is not active' >&2; exit 1; }
OPERATOR_PATH=/home/$OPERATOR/.local/bin:/usr/local/bin:/usr/bin:/bin
POETRY_CACHE_DIR=/srv/cybergym-runtime/cache/poetry

UNIT=/etc/systemd/system/cybergym-campaign.service
TMP=$(mktemp /etc/systemd/system/.cybergym-campaign.XXXXXX)
trap 'rm -f "$TMP"' EXIT HUP INT TERM
cat >"$TMP" <<EOF
[Unit]
Description=CyberGym GPT-5.6 Sol xhigh full-catalog campaign
After=docker.service network-online.target cybergym-server.service
Requires=docker.service cybergym-server.service
Wants=network-online.target

[Service]
Type=simple
User=$OPERATOR
Group=$OPERATOR
SupplementaryGroups=docker
WorkingDirectory=$REPOSITORY_ROOT
Environment=HOME=/home/$OPERATOR
Environment=PATH=$OPERATOR_PATH
Environment=POETRY_CACHE_DIR=$POETRY_CACHE_DIR
ExecStart=$REPOSITORY_ROOT/integrations/hud/ops/cybergym-ops campaign --confirm-paid-all --max-concurrent 4 --shard-size 12
# Resume after a host reboot or abnormal signal, but never loop on an explicit
# campaign/preflight error. The operator inspects and restarts those manually.
Restart=on-abnormal
RestartSec=30
UMask=0077
LimitCORE=0
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/srv/cybergym/results-og-fidelity /srv/cybergym/uv-cache /srv/cybergym-runtime /run /tmp /var/run/docker.sock
TimeoutStopSec=infinity

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$TMP"
mv "$TMP" "$UNIT"
trap - EXIT HUP INT TERM
systemctl daemon-reload
systemctl enable cybergym-campaign.service
systemctl start cybergym-campaign.service
# `campaign.sh` performs the authoritative no-spend preflight inside this
# exact service context. Do not duplicate its full 248-GB read here; any
# failure exits before Job creation/provider spend and leaves the unit failed.
systemctl is-active --quiet cybergym-campaign.service \
    || { printf '%s\n' 'install-campaign-service: campaign did not become active' >&2; exit 1; }
printf '%s\n' 'Installed and started cybergym-campaign.service (gpt-5.6-sol/xhigh, 200 steps, rolling width 4).'

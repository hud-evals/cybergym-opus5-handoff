#!/bin/sh
# Install the pinned, unmasked CyberGym server as a private systemd service.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$DEFAULT_ROOT}
OPERATOR=${SUDO_USER:-rose}
CONFIRM=0

usage() {
    cat <<'EOF'
Usage: sudo install-service.sh --confirm-install [--operator USER] [--repository-root PATH]
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm-install) CONFIRM=1; shift ;;
        --operator) [ "$#" -ge 2 ] || exit 2; OPERATOR=$2; shift 2 ;;
        --repository-root) [ "$#" -ge 2 ] || exit 2; REPOSITORY_ROOT=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'install-service: unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

[ "$CONFIRM" -eq 1 ] || { printf '%s\n' 'install-service: refusing without --confirm-install' >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'install-service: run with sudo' >&2; exit 1; }
id "$OPERATOR" >/dev/null 2>&1 || { printf 'install-service: unknown operator: %s\n' "$OPERATOR" >&2; exit 1; }
[ -x "$REPOSITORY_ROOT/integrations/hud/ops/server.sh" ] \
    || { printf '%s\n' 'install-service: server.sh is missing or not executable' >&2; exit 1; }
[ -r /etc/cybergym/server.env ] \
    || { printf '%s\n' 'install-service: run configure-secrets.sh first' >&2; exit 1; }

UNIT=/etc/systemd/system/cybergym-server.service
TMP=$(mktemp /etc/systemd/system/.cybergym-server.XXXXXX)
trap 'rm -f "$TMP"' EXIT HUP INT TERM
cat >"$TMP" <<EOF
[Unit]
Description=Private unmasked CyberGym fidelity server
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=$OPERATOR
Group=$OPERATOR
SupplementaryGroups=docker
WorkingDirectory=$REPOSITORY_ROOT
EnvironmentFile=/etc/cybergym/server.env
ExecStart=$REPOSITORY_ROOT/integrations/hud/ops/server.sh
Restart=on-failure
RestartSec=5
UMask=0077
LimitCORE=0
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/srv/cybergym/results-og-fidelity /srv/cybergym/uv-cache /srv/cybergym/docker-config /run /tmp /var/run/docker.sock

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$TMP"
mv "$TMP" "$UNIT"
trap - EXIT HUP INT TERM
systemctl daemon-reload
systemctl enable cybergym-server.service
# `enable --now` does not restart an already-active older unit. An explicit
# restart guarantees the live PID now executes this pinned unmasked service.
systemctl restart cybergym-server.service
systemctl is-active --quiet cybergym-server.service \
    || { printf '%s\n' 'install-service: cybergym-server.service did not become active' >&2; exit 1; }
printf '%s\n' 'Installed and started cybergym-server.service (private Docker-bridge bind, unmasked task IDs).'

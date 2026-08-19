#!/bin/sh
# Install restart-safe systemd workers for every planned Daytona lane.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$DEFAULT_ROOT}
OPERATOR=${SUDO_USER:-ubuntu}
CONFIRM=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm-install) CONFIRM=1; shift ;;
        --operator) [ "$#" -ge 2 ] || exit 2; OPERATOR=$2; shift 2 ;;
        --repository-root) [ "$#" -ge 2 ] || exit 2; REPOSITORY_ROOT=$2; shift 2 ;;
        -h|--help)
            printf '%s\n' 'Usage: sudo install-daytona-fleet.sh --confirm-install [--operator USER]'
            exit 0
            ;;
        *) printf 'install-daytona-fleet: unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

[ "$CONFIRM" -eq 1 ] || { printf '%s\n' 'install-daytona-fleet: refusing without --confirm-install' >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'install-daytona-fleet: run with sudo' >&2; exit 1; }
id "$OPERATOR" >/dev/null 2>&1 || { printf 'install-daytona-fleet: unknown operator: %s\n' "$OPERATOR" >&2; exit 1; }
HOME_DIR=$(getent passwd "$OPERATOR" | cut -d: -f6)
[ -n "$HOME_DIR" ] || { printf '%s\n' 'install-daytona-fleet: operator has no home directory' >&2; exit 1; }
RUN_ENV=$HOME_DIR/.config/cybergym/daytona-run.env

UNIT=/etc/systemd/system/cybergym-daytona@.service
TMP=$(mktemp /etc/systemd/system/.cybergym-daytona.XXXXXX)
trap 'rm -f "$TMP"' EXIT HUP INT TERM
cat >"$TMP" <<EOF
[Unit]
Description=CyberGym Daytona Opus 5 lane %i
After=network-online.target cybergym-server.service cybergym-daytona-relay.service cybergym-caddy.service
Requires=cybergym-server.service cybergym-daytona-relay.service cybergym-caddy.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=$OPERATOR
Group=$OPERATOR
WorkingDirectory=$REPOSITORY_ROOT
Environment=HOME=$HOME_DIR
Environment=CYBERGYM_DAYTONA_RUN_ENV=$RUN_ENV
ExecStart=$REPOSITORY_ROOT/integrations/hud/ops/cybergym-ops daytona-lane --lane %i --confirm-paid-selection
Restart=on-failure
RestartSec=60
TimeoutStopSec=infinity
KillMode=control-group
UMask=0077
LimitCORE=0
LimitNOFILE=65536
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/srv/cybergym /srv/cybergym-runtime $HOME_DIR/.config/cybergym /run /tmp

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$TMP"
mv -f "$TMP" "$UNIT"
trap - EXIT HUP INT TERM
systemctl daemon-reload
printf 'Installed cybergym-daytona@.service for operator %s; no paid lane was started.\n' "$OPERATOR"

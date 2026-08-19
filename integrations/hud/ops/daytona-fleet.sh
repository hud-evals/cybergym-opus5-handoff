#!/bin/sh
# Start, inspect, boundary-pause, or resume the complete 24-lane fleet.
set -eu
set +x
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COMMAND=${1:-}
OPERATOR=${SUDO_USER:-$(id -un)}
LANES=${CG_DAYTONA_LANES:-24}

usage() {
    cat <<'EOF'
Usage: sudo daytona-fleet.sh {start|status|pause|resume} [--operator USER]

start/resume cross the paid model boundary. pause is boundary-safe: active
shards finish and checkpoint before each lane exits; it never kills a rollout.
EOF
}

if [ "$COMMAND" = -h ] || [ "$COMMAND" = --help ] || [ -z "$COMMAND" ]; then
    usage
    exit 0
fi
shift
while [ "$#" -gt 0 ]; do
    case "$1" in
        --operator) [ "$#" -ge 2 ] || exit 2; OPERATOR=$2; shift 2 ;;
        *) printf 'daytona-fleet: unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done
case "$COMMAND" in start|status|pause|resume) ;; *) usage >&2; exit 2 ;; esac
if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$SCRIPT_DIR/daytona-fleet.sh" "$COMMAND" --operator "$OPERATOR"
fi
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'daytona-fleet: run with sudo' >&2; exit 1; }
id "$OPERATOR" >/dev/null 2>&1 || { printf 'daytona-fleet: unknown operator: %s\n' "$OPERATOR" >&2; exit 1; }
HOME_DIR=$(getent passwd "$OPERATOR" | cut -d: -f6)
RUN_ENV=$HOME_DIR/.config/cybergym/daytona-run.env
[ -f "$RUN_ENV" ] && [ ! -L "$RUN_ENV" ] || {
    printf '%s\n' 'daytona-fleet: run nix run .#daytona-ready before controlling the fleet' >&2
    exit 1
}

lane_ids() {
    index=1
    while [ "$index" -le "$LANES" ]; do
        printf '%03d\n' "$index"
        index=$((index + 1))
    done
}

run_as_operator() {
    runuser -u "$OPERATOR" -- env HOME="$HOME_DIR" CYBERGYM_DAYTONA_RUN_ENV="$RUN_ENV" "$@"
}

case "$COMMAND" in
    status)
        lane_ids | while IFS= read -r lane; do
            active=$(systemctl show "cybergym-daytona@$lane.service" --property=ActiveState --value)
            printf '{"lane":"%s","service":"%s","campaign":' "$lane" "$active"
            run_as_operator "$SCRIPT_DIR/cybergym-ops" daytona-control status --lane "$lane" | jq -c .
            printf '}\n'
        done
        ;;
    pause)
        lane_ids | while IFS= read -r lane; do
            run_as_operator "$SCRIPT_DIR/cybergym-ops" daytona-control pause --lane "$lane" >/dev/null
        done
        printf '%s\n' 'Pause requested for all lanes. Active shards will finish and checkpoint; no new shard will start.'
        ;;
    start|resume)
        [ -f /etc/systemd/system/cybergym-daytona@.service ] \
            || { printf '%s\n' 'daytona-fleet: fleet service is not installed' >&2; exit 1; }
        if [ "$COMMAND" = resume ]; then
            lane_ids | while IFS= read -r lane; do
                run_as_operator python3 "$SCRIPT_DIR/daytona-control.py" clear \
                    --state-dir "/srv/cybergym/results-og-fidelity/opus5-multilane/lane-$lane/daytona-anthropic/state" \
                    >/dev/null
            done
        fi
        lane_ids | while IFS= read -r lane; do
            systemctl enable "cybergym-daytona@$lane.service" >/dev/null
            if ! systemctl is-active --quiet "cybergym-daytona@$lane.service"; then
                systemctl start "cybergym-daytona@$lane.service"
            fi
        done
        printf 'CyberGym Daytona fleet %s requested for %s lanes. Use `nix run .#daytona -- status`.\n' \
            "$COMMAND" "$LANES"
        ;;
esac

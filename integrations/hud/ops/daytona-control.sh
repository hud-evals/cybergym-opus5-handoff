#!/bin/sh
set -eu
set +x
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COMMAND=${1:-}
if [ "$COMMAND" = "-h" ] || [ "$COMMAND" = "--help" ]; then
    printf '%s\n' 'Usage: daytona-control.sh {status|pause|resume} --lane N [--confirm-paid-selection]'
    exit 0
fi
shift || true
LANE=
CONFIRM=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --lane) LANE=${2:-}; shift 2 ;;
        --confirm-paid-selection) CONFIRM=1; shift ;;
        *) printf 'daytona-control: unknown argument: %s\n' "$1" >&2; exit 2 ;;
    esac
done
case "$COMMAND" in status|pause|resume) ;; *) printf '%s\n' 'usage: daytona-control.sh {status|pause|resume} --lane N [--confirm-paid-selection]' >&2; exit 2 ;; esac
case "$LANE" in ''|*[!0-9]*) printf '%s\n' 'daytona-control: --lane must be a positive integer' >&2; exit 2 ;; esac
[ -n "${CG_RESULTS_DIR-}" ] || { printf '%s\n' 'daytona-control: CG_RESULTS_DIR is required' >&2; exit 1; }
lane=$(printf '%03d' "$LANE")
state_dir=$CG_RESULTS_DIR/lane-$lane/daytona-anthropic/state

case "$COMMAND" in
    status)
        exec python3 "$SCRIPT_DIR/daytona-control.py" status --state-dir "$state_dir"
        ;;
    pause)
        python3 "$SCRIPT_DIR/daytona-control.py" pause --state-dir "$state_dir"
        printf '%s\n' 'Pause requested. The active shard, if any, will finish; no next shard will start.'
        ;;
    resume)
        [ "$CONFIRM" -eq 1 ] || { printf '%s\n' 'daytona-control: resume requires --confirm-paid-selection' >&2; exit 1; }
        python3 "$SCRIPT_DIR/daytona-control.py" clear --state-dir "$state_dir" >/dev/null
        exec "$SCRIPT_DIR/daytona-lane.sh" --lane "$LANE" --confirm-paid-selection
        ;;
esac

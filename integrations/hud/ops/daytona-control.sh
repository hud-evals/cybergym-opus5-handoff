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
lane_number=$(printf '%s\n' "$LANE" | sed 's/^0*//')
[ -n "$lane_number" ] || { printf '%s\n' 'daytona-control: --lane must be positive' >&2; exit 2; }
lane=$(printf '%03d' "$lane_number")

state_dir() {
    printf '%s/pass-%s/lane-%s/daytona-anthropic/state\n' "$CG_RESULTS_DIR" "$1" "$lane"
}

case "$COMMAND" in
    status)
        repeat=1
        while [ "$repeat" -le 3 ]; do
            python3 "$SCRIPT_DIR/daytona-control.py" status --state-dir "$(state_dir "$repeat")" \
                | jq --argjson pass_index "$repeat" '. + {pass_index: $pass_index}'
            repeat=$((repeat + 1))
        done | jq -s '{lane: $lane, repeat_count: 3, passes: ., task_count: (map(.task_count) | add), completed_task_count: (map(.completed_task_count) | add), pending_task_count: (map(.pending_task_count) | add)}' --arg lane "$lane"
        ;;
    pause)
        repeat=1
        while [ "$repeat" -le 3 ]; do
            python3 "$SCRIPT_DIR/daytona-control.py" pause --state-dir "$(state_dir "$repeat")" >/dev/null
            repeat=$((repeat + 1))
        done
        printf '%s\n' 'Pause requested for all repeats. The active shard, if any, will finish; no next shard will start.'
        ;;
    resume)
        [ "$CONFIRM" -eq 1 ] || { printf '%s\n' 'daytona-control: resume requires --confirm-paid-selection' >&2; exit 1; }
        repeat=1
        while [ "$repeat" -le 3 ]; do
            python3 "$SCRIPT_DIR/daytona-control.py" clear --state-dir "$(state_dir "$repeat")" >/dev/null
            repeat=$((repeat + 1))
        done
        exec "$SCRIPT_DIR/daytona-lane.sh" --lane "$LANE" --confirm-paid-selection
        ;;
esac

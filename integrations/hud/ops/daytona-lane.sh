#!/bin/sh
# Run one deterministic Claude Opus 5 Daytona lane from a private plan.
set -eu
set +x
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LANE=
PLAN_DIR=${CG_DAYTONA_PLAN_DIR:-}
RESULTS_ROOT=${CG_RESULTS_DIR:-}
CONFIRM=0

usage() {
  printf '%s\n' 'Usage: daytona-lane.sh --lane N --confirm-paid-selection'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --lane) LANE=${2:-}; shift 2 ;;
    --confirm-paid-selection) CONFIRM=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'daytona-lane: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

case "$LANE" in ''|*[!0-9]*) printf '%s\n' 'daytona-lane: --lane must be a positive integer' >&2; exit 2 ;; esac
[ "$LANE" -ge 1 ] || { printf '%s\n' 'daytona-lane: --lane must be positive' >&2; exit 2; }
[ "$CONFIRM" -eq 1 ] || { printf '%s\n' 'daytona-lane: explicit paid selection confirmation is required' >&2; exit 1; }
[ -n "$PLAN_DIR" ] || { printf '%s\n' 'daytona-lane: CG_DAYTONA_PLAN_DIR is required' >&2; exit 1; }
[ -n "$RESULTS_ROOT" ] || { printf '%s\n' 'daytona-lane: CG_RESULTS_DIR is required' >&2; exit 1; }

lane=$(printf '%03d' "$LANE")
task_file=$PLAN_DIR/lane-$lane.txt
[ -f "$task_file" ] && [ ! -L "$task_file" ] || { printf '%s\n' 'daytona-lane: planned task file is missing' >&2; exit 1; }

export CG_DAYTONA_TASK_FILE=$task_file
export CG_DAYTONA_JOB_NAME=cybergym-opus5-cyber-lane-$lane
export CG_RESULTS_DIR=$RESULTS_ROOT/lane-$lane
exec "$SCRIPT_DIR/daytona-campaign.sh" --confirm-paid-selection

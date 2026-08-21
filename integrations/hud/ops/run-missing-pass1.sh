#!/bin/sh
# Run only CyberGym tasks without a completed numeric Opus 5 result in the
# verified 2026-08-20 HUD pull. The selected campaign is restart-safe.
set -eu
set +x
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TASK_FILE=$SCRIPT_DIR/opus5-missing-pass1-tasks.txt
COMPLETED_FILE=$SCRIPT_DIR/opus5-pass1-completed-from-pull.txt

usage() {
  printf '%s\n' 'Usage: run-missing-pass1.sh --confirm-spend YES'
}

[ "${1:-}" = -h ] || [ "${1:-}" = --help ] && { usage; exit 0; }
[ "${1:-}" = --confirm-spend ] && [ "${2:-}" = YES ] && [ "$#" -eq 2 ] \
  || { usage >&2; exit 2; }
[ "$(wc -l < "$TASK_FILE" | tr -d ' ')" = 1418 ] \
  || { printf '%s\n' 'missing pass@1 task file must contain exactly 1,418 tasks' >&2; exit 1; }
[ "$(wc -l < "$COMPLETED_FILE" | tr -d ' ')" = 89 ] \
  || { printf '%s\n' 'completed-from-pull task file must contain exactly 89 tasks' >&2; exit 1; }
[ -n "${CG_RESULTS_DIR-}" ] \
  || { printf '%s\n' 'run nix run .#daytona-ready first' >&2; exit 1; }
[ -n "${CG_DAYTONA_PREFLIGHT_REPORT-}" ] \
  || { printf '%s\n' 'Daytona preflight report is missing' >&2; exit 1; }

production_root=$CG_RESULTS_DIR
CG_DAYTONA_TASK_FILE=$TASK_FILE
CG_RESULTS_DIR=${CG_MISSING_PASS1_RESULTS_DIR:-$production_root/missing-pass1-from-pull}
CG_DAYTONA_JOB_NAME=cybergym-opus5-cyber
CG_DAYTONA_PROVIDER_CONTROL_ROOT=$CG_RESULTS_DIR/control
export CG_DAYTONA_TASK_FILE CG_RESULTS_DIR CG_DAYTONA_JOB_NAME
export CG_DAYTONA_PROVIDER_CONTROL_ROOT

exec "$SCRIPT_DIR/daytona-campaign.sh" --confirm-paid-selection

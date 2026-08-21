#!/bin/sh
# Run only full-catalog Opus 5 rounds 2 and 3 after pass@1 was uploaded to HUD.
set -eu
set +x
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

usage() {
  printf '%s\n' 'Usage: continue-pass3.sh --confirm-spend YES'
}

case "${1:-}" in -h|--help) usage; exit 0 ;; esac
[ "${1:-}" = --confirm-spend ] && [ "${2:-}" = YES ] && [ "$#" -eq 2 ] \
  || { usage >&2; exit 2; }
[ -n "${CG_RESULTS_DIR-}" ] || { printf '%s\n' 'run nix run .#daytona-ready first' >&2; exit 1; }
[ -n "${CG_DAYTONA_TASK_FILE-}" ] || { printf '%s\n' 'full catalog task file is missing' >&2; exit 1; }
[ "$(wc -l < "$CG_DAYTONA_TASK_FILE" | tr -d ' ')" = 1507 ] \
  || { printf '%s\n' 'continue-pass3 requires the exact 1,507-task full catalog' >&2; exit 1; }

campaign_root=$CG_RESULTS_DIR/continued-after-hud-pass1
full_task_file=$CG_DAYTONA_TASK_FILE
provider_root=$campaign_root/control

for pass_index in 2 3; do
  CG_DAYTONA_TASK_FILE=$full_task_file
  CG_RESULTS_DIR=$campaign_root/pass-$pass_index
  CG_DAYTONA_JOB_NAME=cybergym-opus5-cyber-pass-$pass_index
  CG_DAYTONA_PROVIDER_CONTROL_ROOT=$provider_root
  export CG_DAYTONA_TASK_FILE CG_RESULTS_DIR CG_DAYTONA_JOB_NAME
  export CG_DAYTONA_PROVIDER_CONTROL_ROOT
  "$SCRIPT_DIR/daytona-campaign.sh" --confirm-paid-selection
done

printf '%s\n' 'CyberGym Opus 5 rounds 2 and 3 are complete; pass@1 remains sourced from the previously uploaded HUD jobs.'

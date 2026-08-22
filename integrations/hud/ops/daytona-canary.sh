#!/bin/sh
# Run exactly one paid Opus 5 task in a separate validation namespace.
set -eu
set +x
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

usage() {
    printf '%s\n' 'Usage: daytona-canary.sh --confirm-spend YES'
}

case "${1:-}" in -h|--help) usage; exit 0 ;; esac
[ "${1:-}" = --confirm-spend ] && [ "${2:-}" = YES ] && [ "$#" -eq 2 ] \
    || { usage >&2; exit 2; }
[ -n "${CG_RESULTS_DIR-}" ] || { printf '%s\n' 'run nix run .#daytona-ready first' >&2; exit 1; }
[ -n "${CG_ARTIFACT_PREFLIGHT_REPORT-}" ] || { printf '%s\n' 'artifact preflight report is missing' >&2; exit 1; }
[ -n "${CG_DAYTONA_PREFLIGHT_REPORT-}" ] || { printf '%s\n' 'Daytona preflight report is missing' >&2; exit 1; }

canary_root=${CG_CANARY_RESULTS_DIR:-$CG_RESULTS_DIR/canary-opus5}
input_dir=$canary_root/private-inputs
[ ! -L "$input_dir" ] || { printf '%s\n' 'canary input directory must not be a symlink' >&2; exit 1; }
mkdir -p "$input_dir"
chmod 700 "$input_dir"
task_file=$input_dir/arvo-10400.txt
temporary=$input_dir/.arvo-10400.$$
trap 'rm -f "$temporary"' EXIT HUP INT TERM
printf '%s\n' 'arvo:10400' >"$temporary"
chmod 600 "$temporary"
mv "$temporary" "$task_file"
trap - EXIT HUP INT TERM

CG_DAYTONA_TASK_FILE=$task_file
CG_RESULTS_DIR=$canary_root
CG_DAYTONA_JOB_NAME=cybergym-opus5-cyber-canary
CG_DAYTONA_PROVIDER_CONTROL_ROOT=$canary_root/control
CG_DAYTONA_MAX_CONCURRENT=1
CG_DAYTONA_SHARD_SIZE=1
CG_CAMPAIGN_MAX_CONCURRENT=1
export CG_DAYTONA_TASK_FILE CG_RESULTS_DIR CG_DAYTONA_JOB_NAME
export CG_DAYTONA_PROVIDER_CONTROL_ROOT CG_DAYTONA_MAX_CONCURRENT CG_DAYTONA_SHARD_SIZE
export CG_CAMPAIGN_MAX_CONCURRENT

exec "$SCRIPT_DIR/daytona-campaign.sh" --confirm-paid-selection

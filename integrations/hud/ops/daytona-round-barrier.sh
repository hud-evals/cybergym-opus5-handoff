#!/bin/sh
# Seal one complete Opus 5 repeat before any lane enters the next repeat.
set -eu
set +x
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)}
CAMPAIGN_ROOT=${CG_RESULTS_DIR:-}
PLAN_DIR=${CG_DAYTONA_PLAN_DIR:-}
UV_BIN=${CG_UV_BIN:-uv}
PASS_INDEX=

while [ "$#" -gt 0 ]; do
  case "$1" in
    --pass-index) PASS_INDEX=${2:-}; shift 2 ;;
    -h|--help) printf '%s\n' 'Usage: daytona-round-barrier.sh --pass-index {1|2|3}'; exit 0 ;;
    *) printf 'daytona-round-barrier: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
case "$PASS_INDEX" in 1|2|3) ;; *) printf '%s\n' 'daytona-round-barrier: pass index must be 1, 2, or 3' >&2; exit 2 ;; esac
[ -n "$CAMPAIGN_ROOT" ] || { printf '%s\n' 'daytona-round-barrier: CG_RESULTS_DIR is required' >&2; exit 1; }
[ -n "$PLAN_DIR" ] || { printf '%s\n' 'daytona-round-barrier: CG_DAYTONA_PLAN_DIR is required' >&2; exit 1; }

exec "$UV_BIN" run --frozen --no-sync --project "$REPOSITORY_ROOT/integrations/hud" \
  cybergym-hud-round-barrier \
  --campaign-root "$CAMPAIGN_ROOT" \
  --repository-root "$REPOSITORY_ROOT" \
  --plan-manifest "$PLAN_DIR/manifest.json" \
  --pass-index "$PASS_INDEX" \
  --seal

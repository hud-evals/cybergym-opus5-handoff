#!/bin/sh
# Finalize an exact local CyberGym Opus 5 pass@3 matrix without HUD polling.
set -eu
set +x
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)}
CAMPAIGN_ROOT=${CG_RESULTS_DIR:-}
PLAN_DIR=${CG_DAYTONA_PLAN_DIR:-}
UV_BIN=${CG_UV_BIN:-uv}

[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] || [ "$#" -eq 0 ] || {
  printf '%s\n' 'Usage: daytona-finalize.sh' >&2
  exit 2
}
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  printf '%s\n' 'Usage: daytona-finalize.sh'
  exit 0
fi
[ "$#" -eq 0 ] || { printf '%s\n' 'Usage: daytona-finalize.sh' >&2; exit 2; }
[ -n "$CAMPAIGN_ROOT" ] || { printf '%s\n' 'daytona-finalize: CG_RESULTS_DIR is required' >&2; exit 1; }
[ -n "$PLAN_DIR" ] || { printf '%s\n' 'daytona-finalize: CG_DAYTONA_PLAN_DIR is required' >&2; exit 1; }

exec "$UV_BIN" run --frozen --no-sync --project "$REPOSITORY_ROOT/integrations/hud" \
  cybergym-hud-finalize-pass3 \
  --campaign-root "$CAMPAIGN_ROOT" \
  --repository-root "$REPOSITORY_ROOT" \
  --plan-manifest "$PLAN_DIR/manifest.json"

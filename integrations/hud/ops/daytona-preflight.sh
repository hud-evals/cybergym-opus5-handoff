#!/bin/sh
# No-model Daytona placement, relay, and network proof for the Anthropic lane.
set -eu
set +x
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$DEFAULT_ROOT}
SERVER_URL=${CG_SERVER_URL:-}
RESULTS_DIR=${CG_RESULTS_DIR:-}
KNOWN_HOSTS=${CG_DAYTONA_KNOWN_HOSTS:-$REPOSITORY_ROOT/integrations/hud/daytona_known_hosts.txt}
UV_BIN=${CG_UV_BIN:-uv}

if [ "$#" -gt 0 ]; then
  case "$1" in
    -h|--help)
      printf '%s\n' 'Usage: daytona-preflight.sh'
      exit 0
      ;;
    *)
      printf 'daytona-preflight: unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
fi

[ -n "$SERVER_URL" ] || { printf '%s\n' 'daytona-preflight: CG_SERVER_URL is required' >&2; exit 1; }
[ -n "$RESULTS_DIR" ] || { printf '%s\n' 'daytona-preflight: CG_RESULTS_DIR is required' >&2; exit 1; }
[ -n "${DAYTONA_API_KEY-}" ] || { printf '%s\n' 'daytona-preflight: DAYTONA_API_KEY is required' >&2; exit 1; }
[ -n "${CG_DAYTONA_RELAY_URL-}" ] || { printf '%s\n' 'daytona-preflight: CG_DAYTONA_RELAY_URL is required' >&2; exit 1; }
[ -n "${CG_DAYTONA_RELAY_CIDRS-}" ] || { printf '%s\n' 'daytona-preflight: CG_DAYTONA_RELAY_CIDRS is required' >&2; exit 1; }

destination=$RESULTS_DIR/daytona-anthropic
temporary=$destination/preflight.tmp
report=$destination/preflight.json
install -d -m 0700 "$destination"
rm -f "$temporary"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
"$UV_BIN" run --frozen --no-sync --project "$REPOSITORY_ROOT/integrations/hud" \
  cybergym-hud-preflight-daytona \
  --server "$SERVER_URL" \
  --ledger "$destination/sandboxes.jsonl" \
  --known-hosts "$KNOWN_HOSTS" > "$temporary"
chmod 0600 "$temporary"
mv -f "$temporary" "$report"
trap - EXIT HUP INT TERM
printf 'Daytona Anthropic preflight passed without a model call: %s\n' "$report"

#!/usr/bin/env bash
# Run or reattach the resumable fresh-host bootstrap inside durable tmux.
set -euo pipefail

SESSION=cybergym-bootstrap
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/cybergym
EXIT_RECEIPT=$STATE_DIR/bootstrap.exit

usage() {
    cat <<'EOF'
Usage: nix run .#bootstrap-session [-- --workers 1..32]

Start the CyberGym bootstrap in a named tmux session, or reattach when that
session already exists. The bootstrap continues after SSH disconnects.
EOF
}

case "${1-}" in
    -h|--help) usage; exit 0 ;;
esac

if tmux has-session -t "$SESSION" 2>/dev/null; then
    exec tmux attach-session -t "$SESSION"
fi

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
rm -f "$EXIT_RECEIPT"

printf -v quoted_root '%q' "$REPOSITORY_ROOT"
printf -v quoted_receipt '%q' "$EXIT_RECEIPT"
command="cd $quoted_root && nix run .#bootstrap"
if [ "$#" -gt 0 ]; then
    command+=" --"
    for argument in "$@"; do
        printf -v quoted_argument '%q' "$argument"
        command+=" $quoted_argument"
    done
fi
command+="; rc=\$?; printf '\\nCyberGym bootstrap exited with status %s.\\n' \"\$rc\"; printf '%s\\n' \"\$rc\" > $quoted_receipt; exit \"\$rc\""

exec tmux new-session -s "$SESSION" "$command"

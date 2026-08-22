#!/bin/sh
# Replace only operator-facing credentials on an existing CyberGym host.
# The internal grader key and every non-secret relay/runtime setting are kept.
set -eu
umask 077
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

usage() {
    cat <<'EOF'
Usage: sudo update-secrets.sh [--operator USER]

Prompt privately for HUD_API_KEY, ANTHROPIC_API_KEY, and DAYTONA_API_KEY.
Requires an existing configured host and preserves CYBERGYM_API_KEY plus all
grader, relay, task-file, artifact, and runtime settings.

With --anthropic-only, prompt only for ANTHROPIC_API_KEY and preserve the
existing HUD and Daytona keys.
EOF
}

OPERATOR=${SUDO_USER:-rose}
ANTHROPIC_ONLY=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --operator)
            [ "$#" -ge 2 ] || { printf '%s\n' 'update-secrets: --operator requires a user' >&2; exit 2; }
            OPERATOR=$2
            shift 2
            ;;
        --anthropic-only)
            ANTHROPIC_ONLY=1
            shift
            ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'update-secrets: unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    set -- --operator "$OPERATOR"
    [ "$ANTHROPIC_ONLY" -eq 0 ] || set -- "$@" --anthropic-only
    exec sudo env \
        "PATH=$PATH" \
        "LD_LIBRARY_PATH=${LD_LIBRARY_PATH-}" \
        "$SCRIPT_DIR/update-secrets.sh" "$@"
fi
id "$OPERATOR" >/dev/null 2>&1 || { printf 'update-secrets: unknown operator: %s\n' "$OPERATOR" >&2; exit 1; }
[ -r /dev/tty ] && [ -w /dev/tty ] \
    || { printf '%s\n' 'update-secrets: a controlling TTY is required (use ssh -t)' >&2; exit 1; }

python3 - "$OPERATOR" "$ANTHROPIC_ONLY" <<'PY'
from __future__ import annotations

import getpass
import grp
import os
import pwd
import re
import shlex
import stat
import tempfile
from pathlib import Path

operator = os.sys.argv[1]
anthropic_only = os.sys.argv[2] == "1"
account = pwd.getpwnam(operator)
group = grp.getgrgid(account.pw_gid)
root = Path("/etc/cybergym")
paths = {
    "server": root / "server.env",
    "runner": root / "runner.env",
    "daytona": root / "daytona.env",
}
name_pattern = re.compile(r"^[A-Z][A-Z0-9_]*$")


def prompt_twice(label: str) -> str:
    first = getpass.getpass(f"{label}: ")
    second = getpass.getpass(f"Confirm {label}: ")
    if not first or first != second or "\n" in first or "\r" in first:
        raise SystemExit(f"{label} was invalid or did not match; nothing was written")
    return first


def read_private(path: Path) -> dict[str, str]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) not in {0o600, 0o640}:
        raise SystemExit(f"unsafe existing CyberGym environment file: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise SystemExit(f"CyberGym environment changed while opening: {path}")
        payload = os.read(descriptor, 1024 * 1024 + 1)
    finally:
        os.close(descriptor)
    if len(payload) > 1024 * 1024:
        raise SystemExit(f"CyberGym environment file is unexpectedly large: {path}")
    values: dict[str, str] = {}
    for raw in payload.decode().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, encoded = line.partition("=")
        if separator != "=" or not name_pattern.fullmatch(name):
            raise SystemExit(f"invalid assignment in {path}")
        parsed = shlex.split(encoded, posix=True)
        if len(parsed) != 1 or name in values:
            raise SystemExit(f"ambiguous assignment in {path}: {name}")
        values[name] = parsed[0]
    return values


def write_atomic(path: Path, values: dict[str, str], mode: int) -> None:
    body = "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, 0, account.pw_gid)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


server = read_private(paths["server"])
runner = read_private(paths["runner"])
daytona = read_private(paths["daytona"])
if not server.get("CYBERGYM_API_KEY"):
    raise SystemExit("existing server.env has no CYBERGYM_API_KEY; refusing rotation")

hud_key = ""
daytona_key = ""
if not anthropic_only:
    hud_key = prompt_twice("HUD API key")
anthropic_key = prompt_twice("Anthropic API key")

if not anthropic_only:
    daytona_key = prompt_twice("Daytona API key")
    runner["HUD_API_KEY"] = hud_key
    daytona["DAYTONA_API_KEY"] = daytona_key
runner["ANTHROPIC_API_KEY"] = anthropic_key
runner["CG_MODEL"] = "claude-opus-5"
runner["CG_REASONING_EFFORT"] = ""

# server.env is deliberately never rewritten. The service and task relays keep
# the exact existing CYBERGYM_API_KEY and protected grader identity.
write_atomic(paths["runner"], runner, 0o640)
write_atomic(paths["daytona"], daytona, 0o640)

hud_key = anthropic_key = daytona_key = ""
updated = "Anthropic key" if anthropic_only else "HUD/Anthropic/Daytona keys"
print(f"Updated {updated} for root:{group.gr_name}; internal grader and relay settings were preserved.")
PY

#!/bin/sh
# Enter provider/HUD credentials through a TTY and rotate the private server
# credential locally. Secret values never appear in argv, shell history, or
# script output.
set -eu
umask 077

usage() {
    cat <<'EOF'
Usage: sudo configure-secrets.sh [--operator USER]

Prompt privately for HUD_API_KEY and OPENAI_API_KEY, rotate the internal
CyberGym server key, and atomically write /etc/cybergym/{server,runner}.env.
EOF
}

OPERATOR=${SUDO_USER:-rose}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --operator)
            [ "$#" -ge 2 ] || { printf '%s\n' 'configure-secrets: --operator requires a user' >&2; exit 2; }
            OPERATOR=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'configure-secrets: unknown option: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'configure-secrets: run with sudo' >&2; exit 1; }
id "$OPERATOR" >/dev/null 2>&1 || { printf 'configure-secrets: unknown operator: %s\n' "$OPERATOR" >&2; exit 1; }

# Read from the controlling terminal even when the helper is invoked through
# SSH/sudo or a pipeline. Refuse noninteractive use instead of accepting a key
# from stdin where it could be logged or redirected accidentally.
[ -r /dev/tty ] && [ -w /dev/tty ] \
    || { printf '%s\n' 'configure-secrets: a controlling TTY is required (use ssh -t)' >&2; exit 1; }

python3 - "$OPERATOR" /dev/tty <<'PY'
from __future__ import annotations

import getpass
import grp
import os
import pwd
import secrets
import shlex
import shutil
import tempfile
from pathlib import Path

operator = os.sys.argv[1]
tty_path = os.sys.argv[2]
account = pwd.getpwnam(operator)
group = grp.getgrgid(account.pw_gid)


def prompt_twice(label: str) -> str:
    with open(tty_path, "r+", encoding="utf-8", buffering=1) as tty:
        first = getpass.getpass(f"{label}: ", stream=tty)
        second = getpass.getpass(f"Confirm {label}: ", stream=tty)
    if not first or first != second:
        raise SystemExit(f"{label} was empty or did not match; nothing was written")
    return first


def write_atomic(path: Path, values: dict[str, str], mode: int) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(path.parent, 0, account.pw_gid)
    os.chmod(path.parent, 0o750)
    body = "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, 0, account.pw_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


hud_key = prompt_twice("HUD API key")
openai_key = prompt_twice("OpenAI API key")
private_key = f"cybergym-{secrets.token_urlsafe(32)}"
uv_bin = shutil.which("uv")
if not uv_bin:
    for candidate in (Path("/usr/local/bin/uv"), Path(f"/home/{operator}/.local/bin/uv")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            uv_bin = str(candidate)
            break
if not uv_bin:
    raise SystemExit("uv is not installed; nothing was written")

server = {
    "CYBERGYM_API_KEY": private_key,
    "CG_REPOSITORY_ROOT": "/srv/cybergym/cybergym-og-fidelity-hud",
    "CG_RESULTS_DIR": "/srv/cybergym/results-og-fidelity",
    "CG_SERVER_MODE": "binary",
    "CG_SERVER_BINARY_DIR": "/srv/cybergym/cybergym-server-data",
    "CG_SERVER_PORT": "8666",
    "CG_UV_BIN": uv_bin,
    "UV_CACHE_DIR": "/srv/cybergym/uv-cache",
    "DOCKER_CONFIG": "/srv/cybergym/docker-config",
}
runner = {
    "HUD_API_KEY": hud_key,
    "OPENAI_API_KEY": openai_key,
    "CG_DATA_DIR": "/srv/cybergym/cybergym-data/data",
    "CG_SERVER_URL": "http://172.17.0.1:8666",
    "CG_MODEL": "gpt-4.1-2025-04-14",
    "CG_MODEL_BASE_URL": "",
    "CG_SMOKE_TASK_ID": "arvo:10400",
}

write_atomic(Path("/etc/cybergym/server.env"), server, 0o640)
write_atomic(Path("/etc/cybergym/runner.env"), runner, 0o640)

# Drop references promptly; process exit clears the remaining interpreter state.
hud_key = openai_key = private_key = ""
print(f"Wrote /etc/cybergym/server.env and runner.env for root:{group.gr_name} (values suppressed).")
PY

# Do not restart here: on an existing worker this may still be the incompatible
# old masked service. `install-service.sh` installs the pinned unit and then
# explicitly restarts it onto the rotated key.
printf '%s\n' 'Install/restart the pinned service with ops/install-service.sh before preflight.'

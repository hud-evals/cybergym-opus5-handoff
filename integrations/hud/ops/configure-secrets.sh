#!/bin/sh
# Enter provider/HUD credentials through a TTY and rotate the private server
# credential locally. Secret values never appear in argv, shell history, or
# script output.
set -eu
umask 077

usage() {
    cat <<'EOF'
Usage: sudo configure-secrets.sh [--operator USER] [--repository-root PATH]

Prompt privately for HUD_API_KEY, ANTHROPIC_API_KEY, and DAYTONA_API_KEY,
rotate the internal CyberGym server key, and atomically write the protected
operator environment files under /etc/cybergym.
EOF
}

OPERATOR=${SUDO_USER:-rose}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
while [ "$#" -gt 0 ]; do
    case "$1" in
        --operator)
            [ "$#" -ge 2 ] || { printf '%s\n' 'configure-secrets: --operator requires a user' >&2; exit 2; }
            OPERATOR=$2
            shift 2
            ;;
        --repository-root)
            [ "$#" -ge 2 ] || { printf '%s\n' 'configure-secrets: --repository-root requires a path' >&2; exit 2; }
            REPOSITORY_ROOT=$2
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
[ -d "$REPOSITORY_ROOT/.git" ] || { printf 'configure-secrets: not a Git checkout: %s\n' "$REPOSITORY_ROOT" >&2; exit 1; }
REPOSITORY_ROOT=$(CDPATH= cd -- "$REPOSITORY_ROOT" && pwd -P)

# Read from the controlling terminal even when the helper is invoked through
# SSH/sudo or a pipeline. Refuse noninteractive use instead of accepting a key
# from stdin where it could be logged or redirected accidentally.
[ -r /dev/tty ] && [ -w /dev/tty ] \
    || { printf '%s\n' 'configure-secrets: a controlling TTY is required (use ssh -t)' >&2; exit 1; }

python3 - "$OPERATOR" "$REPOSITORY_ROOT" <<'PY'
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
repository_root = os.sys.argv[2]
account = pwd.getpwnam(operator)
group = grp.getgrgid(account.pw_gid)


def prompt_twice(label: str) -> str:
    # getpass opens /dev/tty itself on Unix. Opening a character device as a
    # seekable update-mode TextIOWrapper ("r+") fails on Python 3.12 before
    # the first prompt with io.UnsupportedOperation. Keep the Python program
    # on the heredoc stdin and let getpass independently own the controlling
    # terminal for hidden input.
    first = getpass.getpass(f"{label}: ")
    second = getpass.getpass(f"Confirm {label}: ")
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
anthropic_key = prompt_twice("Anthropic API key")
daytona_key = prompt_twice("Daytona API key")
private_key = f"cybergym-{secrets.token_urlsafe(32)}"
relay_admin_token = secrets.token_hex(32)
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
    "CG_REPOSITORY_ROOT": repository_root,
    "CG_RESULTS_DIR": "/srv/cybergym/results-og-fidelity",
    "CG_SERVER_MODE": "binary",
    "CG_SERVER_BINARY_DIR": "/srv/cybergym/cybergym-server-data",
    "CG_SERVER_DEPLOYMENT_SEAL": "/etc/cybergym/server-attestation.json",
    "CG_SERVER_PORT": "8666",
    "CG_SERVER_URL": "http://172.30.0.1:8666",
    "CG_RUNTIME_NETWORK": "cybergym-no-internet",
    "CG_UV_BIN": uv_bin,
    "UV_CACHE_DIR": "/srv/cybergym/uv-cache",
    "DOCKER_CONFIG": "/srv/cybergym/docker-config",
}
runner = {
    "HUD_API_KEY": hud_key,
    "ANTHROPIC_API_KEY": anthropic_key,
    "CG_DATA_DIR": "/srv/cybergym-runtime/task-data/cybergym-data/data",
    "CG_DATA_PROVENANCE": "/srv/cybergym-runtime/task-data/provenance/PROVENANCE.json",
    "CG_SERVER_URL": "http://172.30.0.1:8666",
    "CG_RUNTIME_NETWORK": "cybergym-no-internet",
    "CG_MODEL": "claude-opus-5",
    "CG_REASONING_EFFORT": "",
    "CG_JOB_NAME": "cybergym-claude-opus-5-no-internet-v1",
    "CG_MODEL_BASE_URL": "",
    "CG_SMOKE_TASK_ID": "arvo:10400",
    "CG_CAMPAIGN_MAX_CONCURRENT": "4",
    "CG_CAMPAIGN_SHARD_SIZE": "12",
}
daytona = {
    "DAYTONA_API_KEY": daytona_key,
}
relay = {
    "CG_DAYTONA_RELAY_ADMIN_TOKEN": relay_admin_token,
    "CG_DAYTONA_RELAY_REGISTRY": "/srv/cybergym/daytona-relay/registry",
}

write_atomic(Path("/etc/cybergym/server.env"), server, 0o640)
write_atomic(Path("/etc/cybergym/runner.env"), runner, 0o640)
write_atomic(Path("/etc/cybergym/daytona.env"), daytona, 0o640)
write_atomic(Path("/etc/cybergym/relay.env"), relay, 0o640)

# Drop references promptly; process exit clears the remaining interpreter state.
hud_key = anthropic_key = daytona_key = private_key = relay_admin_token = ""
print(f"Wrote protected CyberGym environment files for root:{group.gr_name} (values suppressed).")
PY

# Do not restart here: on an existing worker this may still be the incompatible
# old masked service. `install-service.sh` installs the pinned unit and then
# explicitly restarts it onto the rotated key.
printf '%s\n' 'Install/restart the pinned service with ops/install-service.sh before preflight.'

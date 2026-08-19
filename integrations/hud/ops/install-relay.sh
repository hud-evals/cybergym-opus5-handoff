#!/bin/sh
# Install a public task-scoped HTTPS relay without Tailscale or another account.
set -eu
set +x
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
REPOSITORY_ROOT=${CG_REPOSITORY_ROOT:-$DEFAULT_ROOT}
OPERATOR=${SUDO_USER:-ubuntu}
HOSTNAME=
PUBLIC_IP=
CONFIRM=0

usage() {
    cat <<'EOF'
Usage: sudo install-relay.sh --confirm-public-relay [options]

Install Caddy and the task-scoped CyberGym relay on public HTTPS port 443.
The private grader remains bound only to the Docker-internal gateway.

Options:
  --operator USER
  --repository-root PATH
  --hostname DNS_NAME       Default: PUBLIC-IP.sslip.io
  --public-ip IPV4          Default: detected through api.ipify.org
EOF
}

die() {
    printf 'install-relay: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm-public-relay) CONFIRM=1; shift ;;
        --operator) [ "$#" -ge 2 ] || exit 2; OPERATOR=$2; shift 2 ;;
        --repository-root) [ "$#" -ge 2 ] || exit 2; REPOSITORY_ROOT=$2; shift 2 ;;
        --hostname) [ "$#" -ge 2 ] || exit 2; HOSTNAME=$2; shift 2 ;;
        --public-ip) [ "$#" -ge 2 ] || exit 2; PUBLIC_IP=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'install-relay: unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

[ "$CONFIRM" -eq 1 ] || die 'refusing without --confirm-public-relay'
[ "$(id -u)" -eq 0 ] || die 'run with sudo'
id "$OPERATOR" >/dev/null 2>&1 || die "unknown operator: $OPERATOR"
for path in /etc/cybergym/server.env /etc/cybergym/runner.env /etc/cybergym/relay.env; do
    [ -f "$path" ] && [ ! -L "$path" ] || die "missing protected configuration: $path"
done
[ -x "$REPOSITORY_ROOT/integrations/hud/ops/relay.sh" ] || die 'relay.sh is missing'
CADDY_SOURCE=$(command -v caddy || true)
[ -n "$CADDY_SOURCE" ] && [ -x "$CADDY_SOURCE" ] || die 'caddy is missing from the Nix toolchain'

if [ -z "$PUBLIC_IP" ]; then
    PUBLIC_IP=$(curl -fsS --connect-timeout 10 --max-time 20 https://api.ipify.org)
fi
PUBLIC_IP=$(python3 - "$PUBLIC_IP" <<'PY'
import ipaddress
import sys

value = ipaddress.ip_address(sys.argv[1])
if value.version != 4 or value.is_private or value.is_loopback or value.is_multicast:
    raise SystemExit("public relay address must be a public IPv4 address")
print(value)
PY
)
if [ -z "$HOSTNAME" ]; then
    HOSTNAME=$(printf '%s.sslip.io' "$(printf '%s' "$PUBLIC_IP" | tr . -)")
fi
case "$HOSTNAME" in
    *[!A-Za-z0-9.-]*|.*|*.) die 'relay hostname is malformed' ;;
esac

install -m 0755 -o root -g root "$CADDY_SOURCE" /usr/local/bin/cybergym-caddy
install -d -m 0750 -o "$OPERATOR" -g "$OPERATOR" /srv/cybergym/daytona-relay
install -d -m 0700 -o "$OPERATOR" -g "$OPERATOR" /srv/cybergym/daytona-relay/registry
install -d -m 0700 -o "$OPERATOR" -g "$OPERATOR" /srv/cybergym/caddy-data
install -d -m 0700 -o "$OPERATOR" -g "$OPERATOR" /srv/cybergym/caddy-config

CADDY_TMP=$(mktemp /etc/cybergym/.Caddyfile.XXXXXX)
cat >"$CADDY_TMP" <<EOF
{
    admin off
}

$HOSTNAME {
    encode zstd gzip
    reverse_proxy 127.0.0.1:18765
    header {
        -Server
        X-Content-Type-Options nosniff
        Referrer-Policy no-referrer
    }
}
EOF
chmod 0644 "$CADDY_TMP"
mv -f "$CADDY_TMP" /etc/cybergym/Caddyfile

python3 - "$HOSTNAME" "$PUBLIC_IP" "$OPERATOR" <<'PY'
from __future__ import annotations

import os
import pwd
import shlex
import tempfile
from pathlib import Path

hostname, public_ip, operator = os.sys.argv[1:]
path = Path("/etc/cybergym/runner.env")
values: dict[str, str] = {}
for line in path.read_text(encoding="utf-8").splitlines():
    if not line or line.startswith("#"):
        continue
    parsed = shlex.split(line, comments=False, posix=True)
    if len(parsed) != 1 or "=" not in parsed[0]:
        raise SystemExit("runner.env contains an unsupported line")
    key, value = parsed[0].split("=", 1)
    values[key] = value
values.update(
    {
        "CG_DAYTONA_RELAY_URL": f"https://{hostname}",
        "CG_DAYTONA_RELAY_CIDRS": f"{public_ip}/32",
        "CG_DAYTONA_GRADER_ADMIN_URL": f"https://{hostname}/admin/v1/grader",
    }
)
account = pwd.getpwnam(operator)
body = "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
descriptor, temporary_name = tempfile.mkstemp(prefix=".runner.env.", dir=path.parent, text=True)
try:
    os.fchmod(descriptor, 0o640)
    os.fchown(descriptor, 0, account.pw_gid)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_name, path)
finally:
    try:
        os.unlink(temporary_name)
    except FileNotFoundError:
        pass
PY

RELAY_UNIT_TMP=$(mktemp /etc/systemd/system/.cybergym-daytona-relay.XXXXXX)
cat >"$RELAY_UNIT_TMP" <<EOF
[Unit]
Description=Task-scoped CyberGym Daytona relay
After=network-online.target cybergym-server.service
Requires=cybergym-server.service

[Service]
Type=simple
User=$OPERATOR
Group=$OPERATOR
WorkingDirectory=$REPOSITORY_ROOT
EnvironmentFile=/etc/cybergym/server.env
EnvironmentFile=/etc/cybergym/relay.env
ExecStart=$REPOSITORY_ROOT/integrations/hud/ops/relay.sh
Restart=on-failure
RestartSec=3
UMask=0077
LimitCORE=0
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/srv/cybergym/daytona-relay /srv/cybergym/uv-cache /run /tmp

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$RELAY_UNIT_TMP"
mv -f "$RELAY_UNIT_TMP" /etc/systemd/system/cybergym-daytona-relay.service

CADDY_UNIT_TMP=$(mktemp /etc/systemd/system/.cybergym-caddy.XXXXXX)
cat >"$CADDY_UNIT_TMP" <<EOF
[Unit]
Description=CyberGym public task-relay TLS endpoint
After=network-online.target cybergym-daytona-relay.service
Requires=cybergym-daytona-relay.service

[Service]
Type=simple
User=$OPERATOR
Group=$OPERATOR
ExecStart=/usr/local/bin/cybergym-caddy run --config /etc/cybergym/Caddyfile --adapter caddyfile
Restart=on-failure
RestartSec=3
Environment=XDG_DATA_HOME=/srv/cybergym/caddy-data
Environment=XDG_CONFIG_HOME=/srv/cybergym/caddy-config
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/srv/cybergym/caddy-data /srv/cybergym/caddy-config

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$CADDY_UNIT_TMP"
mv -f "$CADDY_UNIT_TMP" /etc/systemd/system/cybergym-caddy.service

systemctl daemon-reload
systemctl enable cybergym-daytona-relay.service cybergym-caddy.service
systemctl restart cybergym-daytona-relay.service cybergym-caddy.service
systemctl is-active --quiet cybergym-daytona-relay.service || die 'relay backend did not start'
systemctl is-active --quiet cybergym-caddy.service || die 'Caddy TLS endpoint did not start'

printf 'Installed task-scoped relay at https://%s (allow TCP 80/443 to this EC2 instance).\n' "$HOSTNAME"
printf 'Private grader remains at %s and is not bound to the public interface.\n' "$(sed -n 's/^CG_SERVER_URL=//p' /etc/cybergym/runner.env)"

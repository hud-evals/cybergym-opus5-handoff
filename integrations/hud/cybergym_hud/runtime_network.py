"""Fail-closed Docker network policy for CyberGym agent runtimes."""

from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any
from urllib.parse import urlparse

import docker
from docker.errors import NotFound
from docker.types import IPAMConfig, IPAMPool

RUNTIME_NETWORK_POLICY = "docker-internal-no-public-egress-v1"
RUNTIME_NETWORK_NAME = "cybergym-no-internet"
RUNTIME_NETWORK_SUBNET = "172.30.0.0/24"
RUNTIME_NETWORK_GATEWAY = "172.30.0.1"
RUNTIME_NETWORK_LABELS = {
    "ai.hud.cybergym.network-policy": RUNTIME_NETWORK_POLICY,
}
PUBLIC_PROBE_HOSTNAME = "example.com"
PUBLIC_PROBE_IP = "1.1.1.1"
OPENHANDS_RUNTIME_IMAGE = "docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik"


class RuntimeNetworkError(RuntimeError):
    """Raised when the runtime network does not prove complete egress isolation."""


def expected_network_attestation(*, server_url: str) -> dict[str, Any]:
    parsed = urlparse(server_url)
    return {
        "policy": RUNTIME_NETWORK_POLICY,
        "name": RUNTIME_NETWORK_NAME,
        "driver": "bridge",
        "internal": True,
        "enable_ipv6": False,
        "subnet": RUNTIME_NETWORK_SUBNET,
        "gateway": RUNTIME_NETWORK_GATEWAY,
        "server_url": server_url,
        "server_host": parsed.hostname,
        "server_port": parsed.port,
        "private_server_reachable": True,
        "public_ipv4_blocked": True,
        "public_dns_blocked": True,
    }


def network_attestation_sha256(attestation: dict[str, Any]) -> str:
    encoded = json.dumps(attestation, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _network_config(network: Any, *, require_empty: bool) -> dict[str, Any]:
    network.reload()
    attrs = network.attrs or {}
    configs = (attrs.get("IPAM") or {}).get("Config") or []
    if len(configs) != 1 or not isinstance(configs[0], dict):
        raise RuntimeNetworkError("runtime network must have exactly one IPv4 IPAM configuration")
    config = configs[0]
    observed = {
        "name": attrs.get("Name"),
        "driver": attrs.get("Driver"),
        "internal": attrs.get("Internal"),
        "enable_ipv6": attrs.get("EnableIPv6"),
        "subnet": config.get("Subnet"),
        "gateway": config.get("Gateway"),
        "labels": attrs.get("Labels") or {},
    }
    expected = {
        "name": RUNTIME_NETWORK_NAME,
        "driver": "bridge",
        "internal": True,
        "enable_ipv6": False,
        "subnet": RUNTIME_NETWORK_SUBNET,
        "gateway": RUNTIME_NETWORK_GATEWAY,
        "labels": RUNTIME_NETWORK_LABELS,
    }
    if observed != expected:
        raise RuntimeNetworkError(f"runtime network identity drift: expected={expected!r}, observed={observed!r}")
    containers = attrs.get("Containers") or {}
    if require_empty and containers:
        raise RuntimeNetworkError("runtime network has unexpected attached containers before rollout")
    return observed


def ensure_runtime_network(client: Any) -> dict[str, Any]:
    """Create the exact internal network, or reject any same-name drift."""

    try:
        network = client.networks.get(RUNTIME_NETWORK_NAME)
    except NotFound:
        network = client.networks.create(
            RUNTIME_NETWORK_NAME,
            driver="bridge",
            internal=True,
            enable_ipv6=False,
            attachable=False,
            labels=RUNTIME_NETWORK_LABELS,
            ipam=IPAMConfig(
                pool_configs=[
                    IPAMPool(
                        subnet=RUNTIME_NETWORK_SUBNET,
                        gateway=RUNTIME_NETWORK_GATEWAY,
                    )
                ]
            ),
        )
    return _network_config(network, require_empty=True)


def validate_runtime_network(client: Any, *, require_empty: bool = True) -> dict[str, Any]:
    try:
        network = client.networks.get(RUNTIME_NETWORK_NAME)
    except NotFound as exc:
        raise RuntimeNetworkError("runtime network is missing; run the reviewed setup first") from exc
    return _network_config(network, require_empty=require_empty)


def probe_runtime_network(client: Any, *, server_url: str) -> dict[str, Any]:
    """Prove private controller reachability and absence of public IP/DNS egress."""

    parsed = urlparse(server_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != RUNTIME_NETWORK_GATEWAY
        or parsed.port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise RuntimeNetworkError(f"server URL must be the internal-network gateway endpoint, observed {server_url!r}")
    validate_runtime_network(client, require_empty=True)
    script = """
import json
import socket
import sys

server_host, server_port, public_ip, public_hostname = sys.argv[1:]

with socket.create_connection((server_host, int(server_port)), timeout=5):
    private_server_reachable = True

try:
    with socket.create_connection((public_ip, 443), timeout=3):
        public_ipv4_blocked = False
except OSError:
    public_ipv4_blocked = True

try:
    socket.getaddrinfo(public_hostname, 443)
    public_dns_blocked = False
except OSError:
    public_dns_blocked = True

print(json.dumps({
    "private_server_reachable": private_server_reachable,
    "public_ipv4_blocked": public_ipv4_blocked,
    "public_dns_blocked": public_dns_blocked,
}, sort_keys=True))
""".strip()
    try:
        output = client.containers.run(
            OPENHANDS_RUNTIME_IMAGE,
            command=[
                "python",
                "-c",
                script,
                RUNTIME_NETWORK_GATEWAY,
                str(parsed.port),
                PUBLIC_PROBE_IP,
                PUBLIC_PROBE_HOSTNAME,
            ],
            entrypoint=[],
            network=RUNTIME_NETWORK_NAME,
            remove=True,
            stdout=True,
            stderr=False,
            labels={"ai.hud.cybergym.preflight": "runtime-network"},
        )
        observed = json.loads(output.decode("utf-8"))
    except (docker.errors.DockerException, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeNetworkError("runtime network isolation probe failed") from exc
    expected_probe = {
        "private_server_reachable": True,
        "public_ipv4_blocked": True,
        "public_dns_blocked": True,
    }
    if observed != expected_probe:
        raise RuntimeNetworkError(
            f"runtime network did not enforce private-only access: expected={expected_probe!r}, observed={observed!r}"
        )
    validate_runtime_network(client, require_empty=True)
    return expected_network_attestation(server_url=server_url)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ensure or verify the private-only CyberGym runtime network")
    parser.add_argument("action", choices=("ensure", "verify"))
    parser.add_argument("--server")
    return parser


def main() -> None:
    args = _parser().parse_args()
    client = docker.from_env()
    try:
        if args.action == "ensure":
            result = ensure_runtime_network(client)
        else:
            result = validate_runtime_network(client, require_empty=True)
        if args.server:
            result = probe_runtime_network(client, server_url=args.server)
    except (RuntimeNetworkError, docker.errors.DockerException) as exc:
        raise SystemExit(f"runtime-network: {exc}") from exc
    finally:
        client.close()
    print(json.dumps(result, indent=2, sort_keys=True))


__all__ = [
    "RUNTIME_NETWORK_GATEWAY",
    "RUNTIME_NETWORK_LABELS",
    "RUNTIME_NETWORK_NAME",
    "RUNTIME_NETWORK_POLICY",
    "RUNTIME_NETWORK_SUBNET",
    "RuntimeNetworkError",
    "ensure_runtime_network",
    "expected_network_attestation",
    "network_attestation_sha256",
    "probe_runtime_network",
    "validate_runtime_network",
]

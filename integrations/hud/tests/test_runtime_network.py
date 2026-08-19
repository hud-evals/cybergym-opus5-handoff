from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from docker.errors import NotFound

from cybergym_hud.runtime_network import (
    OPENHANDS_RUNTIME_IMAGE,
    RUNTIME_NETWORK_GATEWAY,
    RUNTIME_NETWORK_LABELS,
    RUNTIME_NETWORK_NAME,
    RUNTIME_NETWORK_POLICY,
    RUNTIME_NETWORK_SUBNET,
    RuntimeNetworkError,
    ensure_runtime_network,
    expected_network_attestation,
    network_attestation_sha256,
    probe_runtime_network,
    validate_runtime_network,
)


class FakeNetwork:
    def __init__(self, **overrides: object) -> None:
        self.attrs = {
            "Name": RUNTIME_NETWORK_NAME,
            "Driver": "bridge",
            "Internal": True,
            "EnableIPv6": False,
            "Labels": dict(RUNTIME_NETWORK_LABELS),
            "IPAM": {
                "Config": [
                    {
                        "Subnet": RUNTIME_NETWORK_SUBNET,
                        "Gateway": RUNTIME_NETWORK_GATEWAY,
                    }
                ]
            },
            "Containers": {},
            **overrides,
        }

    def reload(self) -> None:
        return None


class FakeNetworks:
    def __init__(self, network: FakeNetwork | None) -> None:
        self.network = network
        self.created: list[tuple[str, dict[str, object]]] = []

    def get(self, name: str) -> FakeNetwork:
        assert name == RUNTIME_NETWORK_NAME
        if self.network is None:
            raise NotFound("missing")
        return self.network

    def create(self, name: str, **kwargs: object) -> FakeNetwork:
        self.created.append((name, kwargs))
        self.network = FakeNetwork()
        return self.network


class FakeContainers:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload or {
            "private_server_reachable": True,
            "public_ipv4_blocked": True,
            "public_dns_blocked": True,
        }
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, image: str, **kwargs: object) -> bytes:
        self.calls.append((image, kwargs))
        return json.dumps(self.payload).encode()


def _client(
    network: FakeNetwork | None = None,
    *,
    probe: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        networks=FakeNetworks(network or FakeNetwork()),
        containers=FakeContainers(probe),
    )


def test_ensure_creates_exact_internal_network() -> None:
    client = _client()
    client.networks.network = None

    observed = ensure_runtime_network(client)

    assert observed["internal"] is True
    [(name, kwargs)] = client.networks.created
    assert name == RUNTIME_NETWORK_NAME
    assert kwargs["driver"] == "bridge"
    assert kwargs["internal"] is True
    assert kwargs["enable_ipv6"] is False
    assert kwargs["attachable"] is False
    assert kwargs["labels"] == RUNTIME_NETWORK_LABELS


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"Internal": False}, "identity drift"),
        ({"EnableIPv6": True}, "identity drift"),
        ({"Labels": {}}, "identity drift"),
        ({"Containers": {"worker": {}}}, "unexpected attached containers"),
        ({"IPAM": {"Config": [{"Subnet": "172.31.0.0/24", "Gateway": "172.31.0.1"}]}}, "identity drift"),
    ],
)
def test_validate_rejects_network_drift(overrides: dict[str, object], match: str) -> None:
    with pytest.raises(RuntimeNetworkError, match=match):
        validate_runtime_network(_client(FakeNetwork(**overrides)))


def test_probe_proves_private_endpoint_and_blocks_public_network() -> None:
    client = _client()

    result = probe_runtime_network(client, server_url="http://172.30.0.1:8666")

    assert result == expected_network_attestation(server_url="http://172.30.0.1:8666")
    assert result["policy"] == RUNTIME_NETWORK_POLICY
    assert len(network_attestation_sha256(result)) == 64
    [(image, kwargs)] = client.containers.calls
    assert image == OPENHANDS_RUNTIME_IMAGE
    assert kwargs["network"] == RUNTIME_NETWORK_NAME
    assert kwargs["remove"] is True
    assert kwargs["entrypoint"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {
            "private_server_reachable": True,
            "public_ipv4_blocked": False,
            "public_dns_blocked": True,
        },
        {
            "private_server_reachable": True,
            "public_ipv4_blocked": True,
            "public_dns_blocked": False,
        },
    ],
)
def test_probe_rejects_any_public_access(payload: dict[str, object]) -> None:
    with pytest.raises(RuntimeNetworkError, match="did not enforce private-only access"):
        probe_runtime_network(
            _client(probe=payload),
            server_url="http://172.30.0.1:8666",
        )


@pytest.mark.parametrize(
    "server_url",
    [
        "https://172.30.0.1:8666",
        "http://172.17.0.1:8666",
        "http://172.30.0.1:8666/path",
        "http://user:secret@172.30.0.1:8666",
    ],
)
def test_probe_rejects_noncanonical_controller_endpoint(server_url: str) -> None:
    with pytest.raises(RuntimeNetworkError, match="internal-network gateway endpoint"):
        probe_runtime_network(_client(), server_url=server_url)

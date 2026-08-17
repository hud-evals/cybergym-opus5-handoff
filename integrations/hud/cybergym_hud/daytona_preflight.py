"""No-model placement and network proof for the separate Daytona lane."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from .daytona_lane import DAYTONA_IMAGE, prepared_daytona_runtime, validate_daytona_contract


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    validate_daytona_contract()
    with prepared_daytona_runtime(
        task_id="arvo:daytona-preflight",
        server=args.server,
        ledger_path=args.ledger.expanduser().resolve(),
        known_hosts=args.known_hosts.expanduser().resolve(),
    ) as runtime:
        response = httpx.get(f"{runtime.action_url}/alive", timeout=5)
        response.raise_for_status()
        result = {
            "schema_version": "1",
            "no_model_call": True,
            "image": DAYTONA_IMAGE,
            "network_policy": "daytona-funnel-host-cidr-allowlist-task-relay-v1",
            "sandbox_id_recorded": bool(runtime.sandbox_id),
        }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

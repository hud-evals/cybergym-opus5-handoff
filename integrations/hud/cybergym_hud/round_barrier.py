"""Seal a CyberGym Opus 5 repeat only after every planned lane is accepted."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .campaign import CampaignBlocked, _catalog_digest
from .pass3_report import LANE_COUNT, MODEL, _lane_rows, _load_plan
from .scheduler import write_summary

SCHEMA = "cybergym.opus5-round-seal.v1"


def round_status(
    campaign_root: Path,
    *,
    repository_root: Path,
    plan_manifest: Path,
    pass_index: int,
    seal: bool = False,
) -> dict[str, Any]:
    if pass_index not in {1, 2, 3}:
        raise ValueError("pass index must be one, two, or three")
    root = campaign_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    lock_path = root / f"round-{pass_index}.seal.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        catalog, lanes = _load_plan(repository_root.expanduser().resolve(), plan_manifest.expanduser().resolve())
        accepted_rows: list[dict[str, Any]] = []
        pending_lanes: list[int] = []
        for lane in range(1, LANE_COUNT + 1):
            rows = _lane_rows(
                root,
                pass_index=pass_index,
                lane=lane,
                expected_tasks=lanes[lane],
            )
            if rows is None:
                pending_lanes.append(lane)
            else:
                accepted_rows.extend(rows)
        keys = {(row["pass_index"], row["task_id"]) for row in accepted_rows}
        duplicate_count = len(accepted_rows) - len(keys)
        accepted_tasks = {row["task_id"] for row in accepted_rows}
        unknown_count = len(accepted_tasks - set(catalog))
        pending_count = len(catalog) - len(accepted_tasks)
        ready = (
            not pending_lanes
            and len(accepted_rows) == len(catalog)
            and duplicate_count == 0
            and unknown_count == 0
            and pending_count == 0
        )
        payload = {
            "schema": SCHEMA,
            "model": MODEL,
            "pass_index": pass_index,
            "catalog_sha256": _catalog_digest(catalog),
            "task_count": len(catalog),
            "accepted_count": len(accepted_rows),
            "pending_count": pending_count,
            "ambiguous_count": unknown_count,
            "duplicate_count": duplicate_count,
            "pending_lanes": pending_lanes,
            "accepted_rows_sha256": hashlib.sha256(
                json.dumps(accepted_rows, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "sealed": ready,
        }
        seal_path = root / f"round-{pass_index}.sealed.json"
        if seal_path.exists():
            existing = json.loads(seal_path.read_text())
            if existing != payload or not ready:
                raise CampaignBlocked(f"round {pass_index} seal no longer matches authoritative manifests")
        elif seal and ready:
            write_summary(seal_path, payload)
        payload["seal_path"] = str(seal_path)
        return payload
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--plan-manifest", type=Path, required=True)
    parser.add_argument("--pass-index", type=int, required=True)
    parser.add_argument("--seal", action="store_true")
    args = parser.parse_args()
    result = round_status(
        args.campaign_root,
        repository_root=args.repository_root,
        plan_manifest=args.plan_manifest,
        pass_index=args.pass_index,
        seal=args.seal,
    )
    print(json.dumps(result, sort_keys=True))
    if not result["sealed"]:
        raise SystemExit(75)


if __name__ == "__main__":
    main()


__all__ = ["round_status"]

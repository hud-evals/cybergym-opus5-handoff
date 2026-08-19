#!/usr/bin/env python3
"""Inspect, pause at a shard boundary, or clear a CyberGym lane pause."""

from __future__ import annotations

import argparse
import json
import os
import stat
from collections import Counter
from pathlib import Path

PAUSE_CONTENT = b"pause-after-current-shard-v1\n"


def pause_path(state_dir: Path) -> Path:
    return state_dir / "pause.requested"


def request_pause(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    path = pause_path(state_dir)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or path.read_bytes() != PAUSE_CONTENT:
            raise RuntimeError("existing pause request differs")
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, PAUSE_CONTENT)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def clear_pause(state_dir: Path) -> None:
    path = pause_path(state_dir)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or path.read_bytes() != PAUSE_CONTENT:
        raise RuntimeError("pause request is malformed or unsafe")
    path.unlink()


def status(state_dir: Path) -> dict[str, object]:
    manifest_path = state_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    counts: Counter[str] = Counter()
    completed = 0
    total = 0
    running_attempts = []
    halt = None
    if isinstance(manifest, dict):
        halt = manifest.get("halt")
        for shard in manifest.get("shards") or []:
            total += len(shard.get("task_ids") or [])
            completed += len(shard.get("completed_task_ids") or [])
            counts[str(shard.get("status"))] += 1
            for attempt in shard.get("attempts") or []:
                if attempt.get("status") == "running":
                    running_attempts.append(
                        {
                            "shard_index": shard.get("index"),
                            "job_id": attempt.get("job_id"),
                            "launched": len(attempt.get("launched_task_ids") or []),
                            "native_returned": len(attempt.get("native_returned_task_ids") or []),
                        }
                    )
    path = pause_path(state_dir)
    try:
        pause_metadata = path.lstat()
    except FileNotFoundError:
        paused = False
    else:
        paused = stat.S_ISREG(pause_metadata.st_mode) and path.read_bytes() == PAUSE_CONTENT
    return {
        "state_dir": str(state_dir),
        "manifest_exists": manifest is not None,
        "pause_requested": paused,
        "task_count": total,
        "completed_task_count": completed,
        "pending_task_count": total - completed,
        "shard_status_counts": dict(sorted(counts.items())),
        "running_attempts": running_attempts,
        "halt": halt,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "pause", "clear"))
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    state_dir = args.state_dir.expanduser().resolve()
    if args.command == "pause":
        request_pause(state_dir)
    elif args.command == "clear":
        clear_pause(state_dir)
    print(json.dumps(status(state_dir), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

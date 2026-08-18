#!/usr/bin/env python3
"""Create deterministic, disjoint Daytona lane task files for Claude Opus 5."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path

from cybergym_hud.taskset import task_ids as catalog_task_ids

MODEL = "claude-opus-5"
JOB_PREFIX = "cybergym-opus5-cyber"
MAX_LANE_CONCURRENCY = 60
MAX_INPUT_BYTES = 16 * 1024 * 1024


def read_private(path: Path) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 or before.st_size > MAX_INPUT_BYTES:
        raise RuntimeError(f"lane planner input must be a private bounded regular file: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise RuntimeError("lane planner input changed while opening")
        payload = b""
        while len(payload) <= MAX_INPUT_BYTES:
            block = os.read(
                descriptor,
                min(1024 * 1024, MAX_INPUT_BYTES + 1 - len(payload)),
            )
            if not block:
                break
            payload += block
    finally:
        os.close(descriptor)
    if len(payload) > MAX_INPUT_BYTES:
        raise RuntimeError("lane planner input exceeds its safety limit")
    after = path.lstat()
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ):
        raise RuntimeError("lane planner input changed while reading")
    return payload


def write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise RuntimeError(f"existing lane artifact differs: {path}")
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while persisting Daytona lane plan")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--lanes", type=int, required=True)
    parser.add_argument("--max-concurrent", type=int, default=35)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.lanes < 1 or args.lanes > 128:
        raise RuntimeError("--lanes must be between 1 and 128")
    if not 1 <= args.max_concurrent <= MAX_LANE_CONCURRENCY:
        raise RuntimeError(f"--max-concurrent must be between 1 and {MAX_LANE_CONCURRENCY}")
    selected = tuple(
        line.strip() for line in read_private(args.task_file.resolve()).decode().splitlines() if line.strip()
    )
    if not selected or len(selected) != len(set(selected)):
        raise RuntimeError("lane planner task selection must be nonempty and unique")
    catalog = catalog_task_ids(Path(__file__).resolve().parents[3])
    selected_set = set(selected)
    if tuple(task_id for task_id in catalog if task_id in selected_set) != selected:
        raise RuntimeError("lane planner task selection is unknown or out of catalog order")

    lanes = {index: tuple(selected[index :: args.lanes]) for index in range(args.lanes)}
    flattened = tuple(task_id for index in range(args.lanes) for task_id in lanes[index])
    if len(flattened) != len(selected) or set(flattened) != selected_set:
        raise RuntimeError("Daytona lane plan is not an exact disjoint partition")

    manifest = {
        "schema_version": "1",
        "model": MODEL,
        "job_prefix": JOB_PREFIX,
        "task_count": len(selected),
        "source_task_file_sha256": hashlib.sha256(("\n".join(selected) + "\n").encode()).hexdigest(),
        "lane_count": args.lanes,
        "max_concurrent_per_lane": args.max_concurrent,
        "combined_max_concurrent": args.lanes * args.max_concurrent,
        "lanes": [],
    }
    for index in range(args.lanes):
        lane_number = index + 1
        payload = ("\n".join(lanes[index]) + "\n").encode()
        path = args.output_dir.resolve() / f"lane-{lane_number:03d}.txt"
        write_private(path, payload)
        manifest["lanes"].append(
            {
                "lane": lane_number,
                "task_file": str(path),
                "task_count": len(lanes[index]),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "job_name": f"{JOB_PREFIX}-lane-{lane_number:03d}",
            }
        )
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    write_private(args.output_dir.resolve() / "manifest.json", encoded)
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

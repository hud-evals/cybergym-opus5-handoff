#!/usr/bin/env python3
"""Write the exact full CyberGym catalog to a private Daytona task file."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from cybergym_hud.taskset import task_ids


def write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise RuntimeError(f"existing Daytona task selection differs: {path}")
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while saving Daytona task selection")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    selected = task_ids(args.repository_root.resolve())
    if len(selected) != 1507 or len(set(selected)) != 1507:
        raise RuntimeError("canonical CyberGym catalog is not exactly 1,507 unique tasks")
    payload = ("\n".join(selected) + "\n").encode()
    write_private(args.output.resolve(), payload)
    print(f"Wrote {len(selected)} canonical task IDs to {args.output.resolve()}")


if __name__ == "__main__":
    main()

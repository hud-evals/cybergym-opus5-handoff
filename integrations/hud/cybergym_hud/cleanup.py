"""Narrow cleanup for trace-private OpenHands workspace roots."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import docker

OPENHANDS_RUNTIME_IMAGE = "docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik"


def cleanup_tracked_root(
    root: str | Path,
    *,
    docker_client_factory: Callable[[], Any] = docker.from_env,
) -> None:
    """Remove one scheduler-created root, using Docker only for root-owned children.

    The OpenHands runtime can leave its bind-mounted workspace owned by root.
    The fallback mounts only the already-validated trace-private root, disables
    networking, drops capabilities, and removes children without following
    symlinks.  A failed cleanup is an infrastructure error, never silent.
    """

    path = Path(root)
    if not path.exists():
        return
    if path.is_symlink() or not path.is_dir() or not path.name.startswith("hud-rollout-"):
        raise RuntimeError(f"refusing cleanup outside a trace-private rollout root: {path}")

    shutil.rmtree(path, ignore_errors=True)
    if not path.exists():
        return

    client = docker_client_factory()
    try:
        client.containers.run(
            OPENHANDS_RUNTIME_IMAGE,
            command=["/bin/sh", "-c", "find /cleanup -mindepth 1 -delete"],
            volumes={str(path.resolve()): {"bind": "/cleanup", "mode": "rw"}},
            network_disabled=True,
            read_only=True,
            cap_drop=["ALL"],
            # Required to remove root-owned 0760 children beneath an
            # operator-owned 0700 trace root; no broader capability is kept.
            cap_add=["DAC_OVERRIDE"],
            security_opt=["no-new-privileges"],
            user="0:0",
            remove=True,
        )
    finally:
        client.close()

    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        raise RuntimeError(f"trace-private OpenHands workspace cleanup failed: {path}")

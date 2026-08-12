"""Taskset factories over the pinned 1,507-task catalog."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from hud import Taskset

from .contract import CONTRACT, repository_root
from .tasks import make_task


def task_ids(root: str | Path | None = None) -> tuple[str, ...]:
    mapping = json.loads((repository_root(root) / "mask_map.json").read_text(encoding="utf-8"))
    if not isinstance(mapping, dict):
        raise RuntimeError("CyberGym mask_map.json must be an object")
    ids = tuple(sorted(mapping))
    if len(ids) != CONTRACT["benchmark"]["task_count"]:
        raise RuntimeError("CyberGym catalog cardinality drifted")
    return ids


def make_taskset(
    *,
    server: str,
    selected: Iterable[str] | None = None,
    root: str | Path | None = None,
) -> Taskset:
    catalog = task_ids(root)
    ids = tuple(selected) if selected is not None else catalog
    unknown = sorted(set(ids).difference(catalog))
    if unknown:
        raise ValueError(f"unknown CyberGym task IDs: {unknown}")
    return Taskset("cybergym-og-native-level1", [make_task(task_id, server=server) for task_id in ids])


__all__ = ["make_taskset", "task_ids"]

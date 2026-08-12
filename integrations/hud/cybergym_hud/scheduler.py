"""One-shot native OpenHands scheduler that returns a HUD Job receipt."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from hud.eval.runtime import LocalRuntime

from .contract import validate_contract
from .env import build_env
from .native import NativeOpenHandsAgent, NativeOpenHandsConfig
from .taskset import make_taskset


def prepare_tracked_rollout(
    config: NativeOpenHandsConfig,
    *,
    uuid_factory: Callable[[], UUID] = uuid4,
) -> tuple[NativeOpenHandsConfig, bool]:
    """Reserve one trace-private upstream tmp root and defer its cleanup."""

    original = config.normalized()
    original.tmp_dir.mkdir(parents=True, exist_ok=True)
    tracking_root = original.tmp_dir / f"hud-rollout-{uuid_factory().hex}"
    tracking_root.mkdir(exist_ok=False)
    return replace(original, tmp_dir=tracking_root, remove_tmp=False), original.remove_tmp


def summarize_job(job: Any) -> dict[str, Any]:
    if len(job.runs) != 1:
        raise RuntimeError(f"one-shot scheduler expected one run, got {len(job.runs)}")
    run = job.runs[0]
    receipt: dict[str, Any] | None = None
    if run.trace.content:
        try:
            parsed = json.loads(run.trace.content)
            receipt = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            receipt = None
    return {
        "job_id": job.id,
        "trace_id": run.trace_id,
        "task_slug": run.slug,
        "status": run.trace.status,
        "reward": run.reward,
        "is_error": run.grade.is_error or run.trace.is_error,
        "evaluation": run.evaluation,
        "native_receipt": receipt,
    }


async def run_one(task_id: str, config: NativeOpenHandsConfig) -> dict[str, Any]:
    config = config.normalized()
    validate_contract(root=config.repository_root)
    rollout_config, cleanup_after_rollout = prepare_tracked_rollout(config)
    try:
        taskset = make_taskset(
            server=rollout_config.server,
            selected=[task_id],
            root=rollout_config.repository_root,
        )
        agent = NativeOpenHandsAgent(rollout_config)
        job = await taskset.run(
            agent,
            runtime=LocalRuntime(lambda _task: build_env(file_tracking_root=rollout_config.tmp_dir)),
            max_concurrent=1,
        )
        return summarize_job(job)
    finally:
        # The HUD observer flushes before taskset.run returns. Cleanup here
        # cannot erase the model's final workspace before telemetry captures it.
        if cleanup_after_rollout:
            shutil.rmtree(rollout_config.tmp_dir, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one pinned CyberGym task through upstream OpenHands and emit a HUD receipt"
    )
    parser.add_argument("task_id")
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--tmp-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--max-iter", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--keep-tmp", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = NativeOpenHandsConfig(
        repository_root=args.repository_root,
        data_dir=args.data_dir,
        server=args.server,
        model=args.model,
        log_dir=args.log_dir,
        tmp_dir=args.tmp_dir,
        max_iter=args.max_iter,
        timeout=args.timeout,
        base_url=args.base_url,
        top_p=args.top_p,
        temperature=args.temperature,
        max_output_tokens=args.max_output_tokens,
        seed=args.seed,
        silent=args.silent,
        remove_tmp=not args.keep_tmp,
        debug=args.debug,
    )
    result = asyncio.run(run_one(args.task_id, config))
    print(json.dumps(result, indent=2, default=str))
    if result["is_error"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

"""One-shot and rolling-batch native OpenHands HUD receipt scheduler."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from hud.eval.runtime import LocalRuntime

from .contract import validate_contract
from .env import build_env
from .native import NativeOpenHandsAgent, NativeOpenHandsBatchAgent, NativeOpenHandsConfig
from .receipt import NativeReceipt, NativeTaskBinding
from .taskset import make_taskset
from .taskset import task_ids as catalog_task_ids

DEFAULT_MAX_CONCURRENT = 15


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


def _summarize_run(run: Any, *, job_id: str) -> dict[str, Any]:
    receipt: dict[str, Any] | None = None
    if run.trace.content:
        try:
            parsed = json.loads(run.trace.content)
            receipt = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            receipt = None
    return {
        "job_id": job_id,
        "trace_id": run.trace_id,
        "task_slug": run.slug,
        "status": run.trace.status,
        "reward": run.reward,
        "is_error": run.grade.is_error or run.trace.is_error,
        "evaluation": run.evaluation,
        "native_receipt": receipt,
    }


def summarize_job(job: Any) -> dict[str, Any]:
    if len(job.runs) != 1:
        raise RuntimeError(f"one-shot scheduler expected one run, got {len(job.runs)}")
    return _summarize_run(job.runs[0], job_id=job.id)


def summarize_batch_job(job: Any) -> dict[str, Any]:
    """Return one ordered summary while preserving every native run receipt."""

    runs = [_summarize_run(run, job_id=job.id) for run in job.runs]
    rewards = [float(run["reward"] or 0.0) for run in runs]
    return {
        "job_id": job.id,
        "task_count": len(runs),
        "error_count": sum(bool(run["is_error"]) for run in runs),
        "reward_sum": sum(rewards),
        "mean_reward": sum(rewards) / len(rewards) if rewards else 0.0,
        "is_error": any(bool(run["is_error"]) for run in runs),
        "runs": runs,
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
            runtime=LocalRuntime(
                lambda _task: build_env(
                    file_tracking_root=rollout_config.tmp_dir,
                    cleanup_file_tracking_root=cleanup_after_rollout,
                )
            ),
            max_concurrent=1,
        )
        return summarize_job(job)
    finally:
        # The HUD observer flushes before taskset.run returns. Cleanup here
        # cannot erase the model's final workspace before telemetry captures it.
        if cleanup_after_rollout:
            shutil.rmtree(rollout_config.tmp_dir, ignore_errors=True)


async def run_many(
    task_ids: Iterable[str],
    config: NativeOpenHandsConfig,
    *,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    executor: Callable[[NativeOpenHandsConfig, NativeTaskBinding], NativeReceipt] | None = None,
    uuid_factory: Callable[[], UUID] = uuid4,
) -> dict[str, Any]:
    """Run a rolling batch with one isolated upstream configuration per row."""

    selected = tuple(task_ids)
    if not selected:
        raise ValueError("run_many requires at least one task ID")
    if len(selected) != len(set(selected)):
        raise ValueError("run_many does not accept duplicate task IDs")
    if not 1 <= max_concurrent <= DEFAULT_MAX_CONCURRENT:
        raise ValueError(f"max_concurrent must be between 1 and {DEFAULT_MAX_CONCURRENT}")

    config = config.normalized()
    validate_contract(root=config.repository_root)
    taskset = make_taskset(server=config.server, selected=selected, root=config.repository_root)

    rollout_configs: dict[str, NativeOpenHandsConfig] = {}
    cleanup_roots: dict[str, bool] = {}
    try:
        for task_id in selected:
            rollout_config, cleanup_after_rollout = prepare_tracked_rollout(
                config,
                uuid_factory=uuid_factory,
            )
            rollout_configs[task_id] = rollout_config
            cleanup_roots[task_id] = cleanup_after_rollout

        def environment_for(task: Any):
            raw_task_id = task.args.get("task_id")
            if not isinstance(raw_task_id, str) or raw_task_id not in rollout_configs:
                raise ValueError(f"scheduled row has no isolated rollout config: {raw_task_id!r}")
            rollout_config = rollout_configs[raw_task_id]
            return build_env(
                file_tracking_root=rollout_config.tmp_dir,
                cleanup_file_tracking_root=cleanup_roots[raw_task_id],
            )

        job = await taskset.run(
            NativeOpenHandsBatchAgent(rollout_configs, executor=executor),
            runtime=LocalRuntime(environment_for),
            # This is the only concurrency controller: HUD releases the next
            # waiting rollout as soon as any active slot completes.
            max_concurrent=max_concurrent,
        )
        return summarize_batch_job(job)
    finally:
        # Normal cleanup happens in each LocalRuntime shutdown, after that
        # rollout's file-tracking observer flush. This is only a failure or
        # cancellation fallback for roots whose runtime never shut down.
        for task_id, rollout_config in rollout_configs.items():
            if cleanup_roots[task_id]:
                shutil.rmtree(rollout_config.tmp_dir, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run pinned CyberGym tasks through upstream OpenHands and emit HUD receipts"
    )
    parser.add_argument("task_ids", nargs="*", metavar="TASK_ID")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="run the complete 1,507-task catalog")
    selection.add_argument("--first-n", type=int, metavar="N", help="run the first N sorted catalog tasks")
    parser.add_argument(
        "--confirm-paid-all",
        action="store_true",
        help="required whenever the selection covers the complete paid catalog",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=DEFAULT_MAX_CONCURRENT,
        help=f"rolling native rollout limit (1-{DEFAULT_MAX_CONCURRENT}; default: %(default)s)",
    )
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


def _resolve_selection(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[str, ...]:
    catalog = catalog_task_ids(args.repository_root)
    if args.task_ids and (args.all or args.first_n is not None):
        parser.error("positional task IDs cannot be combined with --all or --first-n")
    if args.all:
        selected = catalog
    elif args.first_n is not None:
        if args.first_n < 1:
            parser.error("--first-n must be at least one")
        if args.first_n > len(catalog):
            parser.error(f"--first-n cannot exceed the {len(catalog)}-task catalog")
        selected = catalog[: args.first_n]
    elif args.task_ids:
        selected = tuple(args.task_ids)
    else:
        parser.error("select one or more TASK_ID values, --first-n N, or --all")

    if len(selected) != len(set(selected)):
        parser.error("duplicate task IDs are not allowed")
    if set(selected) == set(catalog) and not args.confirm_paid_all:
        parser.error("the complete paid catalog requires --confirm-paid-all")
    if not 1 <= args.max_concurrent <= DEFAULT_MAX_CONCURRENT:
        parser.error(f"--max-concurrent must be between 1 and {DEFAULT_MAX_CONCURRENT}")
    return selected


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    selected = _resolve_selection(args, parser)
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
    if len(selected) == 1:
        result = asyncio.run(run_one(selected[0], config))
    else:
        result = asyncio.run(run_many(selected, config, max_concurrent=args.max_concurrent))
    print(json.dumps(result, indent=2, default=str))
    if result["is_error"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

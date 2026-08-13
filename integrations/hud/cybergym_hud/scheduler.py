"""One-shot and rolling-batch native OpenHands HUD receipt scheduler."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from hud.eval import Job
from hud.eval.runtime import LocalRuntime
from hud.settings import settings
from hud.utils.platform import PlatformClient

from .cleanup import cleanup_tracked_root
from .contract import validate_contract
from .env import build_env
from .native import NativeOpenHandsAgent, NativeOpenHandsBatchAgent, NativeOpenHandsConfig
from .receipt import NativeReceipt, NativeTaskBinding
from .taskset import make_taskset
from .taskset import task_ids as catalog_task_ids

DEFAULT_MAX_CONCURRENT = 15


def _validate_job_name(job_name: str | None) -> str | None:
    """Validate an optional human-facing HUD Job name."""

    if job_name is None:
        return None
    if not job_name or job_name != job_name.strip():
        raise ValueError("job_name must be non-empty with no surrounding whitespace")
    if len(job_name) > 128 or any(ord(character) < 32 or ord(character) == 127 for character in job_name):
        raise ValueError("job_name must be at most 128 printable characters")
    return job_name


async def _start_named_job(taskset: Any, job_name: str | None) -> Job | None:
    name = _validate_job_name(job_name)
    if name is None:
        return None
    return await Job.start(name, taskset_id=taskset.api_id)


def _trace_key(value: object) -> str:
    """Normalize API UUIDs and local uuid4().hex IDs for comparison."""

    rendered = str(value)
    try:
        return UUID(rendered).hex
    except ValueError:
        return rendered


async def require_remote_hud_receipt(job_id: str, trace_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Fail closed unless HUD exposes terminal, rewarded rows and events."""

    expected = {_trace_key(trace_id) for trace_id in trace_ids}
    client = PlatformClient.from_settings()
    remote: dict[str, tuple[str, dict[str, Any]]] = {}
    terminal: set[str] = set()
    for attempt in range(3):
        rows: list[Any] = []
        # HUD caps this endpoint at 1,000 rows. The complete CyberGym catalog
        # has 1,507 traces, so fetch pages instead of discovering this only
        # after a fully paid campaign.
        page_limit = 1000
        for offset in range(0, max(len(trace_ids), 1), page_limit):
            data = await client.aget(
                f"/jobs/{job_id}/traces",
                params={"limit": min(page_limit, max(len(trace_ids) - offset, 1)), "offset": offset},
            )
            page = data if isinstance(data, list) else data.get("items", [])
            if isinstance(page, list):
                rows.extend(page)
        remote = {
            _trace_key(row.get("id")): (str(row.get("id")), row)
            for row in rows
            if isinstance(row, dict) and row.get("id") is not None
        }
        terminal = {
            trace_id
            for trace_id, (_remote_id, row) in remote.items()
            if row.get("status") in {"completed", "error", "cancelled"} and row.get("reward") is not None
        }
        if expected.issubset(terminal):
            break
        if attempt < 2:
            await asyncio.sleep(1.0)
    if not expected.issubset(terminal):
        missing = sorted(expected.difference(remote))
        nonterminal = sorted(expected.intersection(remote).difference(terminal))
        raise RuntimeError(
            "HUD did not expose terminal rewarded receipts after telemetry flush: "
            f"missing={missing}, nonterminal={nonterminal}"
        )

    remote_ids = tuple(remote[_trace_key(trace_id)][0] for trace_id in trace_ids)
    missing_events: list[str] = []
    for trace_id in remote_ids:
        observed = False
        for attempt in range(3):
            data = await client.aget(f"/trace/{trace_id}/events")
            events = data.get("events", []) if isinstance(data, dict) else []
            if isinstance(events, list) and events:
                observed = True
                break
            if attempt < 2:
                await asyncio.sleep(1.0)
        if not observed:
            missing_events.append(trace_id)
    if missing_events:
        raise RuntimeError("HUD receipts lack remotely readable telemetry events: " + ", ".join(missing_events))
    return remote_ids


def write_summary(path: Path, payload: dict[str, Any]) -> None:
    """Atomically retain a non-secret local receipt before remote polling."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n").encode()
    temporary = path.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write to CyberGym summary")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


async def verify_and_persist_remote_receipt(result: dict[str, Any], *, results_dir: Path) -> dict[str, Any]:
    """Persist false-first HUD state, then replace it only after API proof."""

    raw_runs = result.get("runs") if isinstance(result.get("runs"), list) else [result]
    trace_ids = tuple(str(run["trace_id"]) for run in raw_runs if isinstance(run, dict) and run.get("trace_id"))
    if len(trace_ids) != len(raw_runs):
        raise RuntimeError("one or more completed HUD runs omitted a trace ID")
    web = settings.hud_web_url.rstrip("/")
    summary: dict[str, Any] = {
        **result,
        "job_url": f"{web}/jobs/{result['job_id']}",
        "trace_ids": list(trace_ids),
        "trace_urls": [f"{web}/trace/{trace_id}" for trace_id in trace_ids],
        "hud_remote_receipt_verified": False,
        "hud_remote_events_verified": False,
    }
    summary_path = results_dir / f"hud-summary-{result['job_id']}.json"
    write_summary(summary_path, summary)
    remote_ids = await require_remote_hud_receipt(str(result["job_id"]), trace_ids)
    summary.update(
        {
            "trace_ids": list(remote_ids),
            "trace_urls": [f"{web}/trace/{trace_id}" for trace_id in remote_ids],
            "hud_remote_receipt_verified": True,
            "hud_remote_events_verified": True,
            "summary_path": str(summary_path),
        }
    )
    write_summary(summary_path, summary)
    return summary


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


def _summarize_run(run: Any, *, job_id: str, job_name: str) -> dict[str, Any]:
    receipt: dict[str, Any] | None = None
    if run.trace.content:
        try:
            parsed = json.loads(run.trace.content)
            receipt = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            receipt = None
    return {
        "job_id": job_id,
        "job_name": job_name,
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
    return _summarize_run(job.runs[0], job_id=job.id, job_name=job.name)


def summarize_batch_job(job: Any) -> dict[str, Any]:
    """Return one ordered summary while preserving every native run receipt."""

    runs = [_summarize_run(run, job_id=job.id, job_name=job.name) for run in job.runs]
    rewards = [float(run["reward"] or 0.0) for run in runs]
    return {
        "job_id": job.id,
        "job_name": job.name,
        "task_count": len(runs),
        "error_count": sum(bool(run["is_error"]) for run in runs),
        "reward_sum": sum(rewards),
        "mean_reward": sum(rewards) / len(rewards) if rewards else 0.0,
        "is_error": any(bool(run["is_error"]) for run in runs),
        "runs": runs,
    }


async def run_one(
    task_id: str,
    config: NativeOpenHandsConfig,
    *,
    job_name: str | None = None,
) -> dict[str, Any]:
    config = config.normalized()
    validate_contract(root=config.repository_root)
    rollout_config, cleanup_after_rollout = prepare_tracked_rollout(config)
    worker_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cybergym-native")
    try:
        taskset = make_taskset(
            server=rollout_config.server,
            selected=[task_id],
            root=rollout_config.repository_root,
        )
        job = await _start_named_job(taskset, job_name)
        agent = NativeOpenHandsAgent(rollout_config, worker_pool=worker_pool)
        job = await taskset.run(
            agent,
            runtime=LocalRuntime(
                lambda _task: build_env(
                    file_tracking_root=rollout_config.tmp_dir,
                    cleanup_file_tracking_root=cleanup_after_rollout,
                )
            ),
            max_concurrent=1,
            job=job,
        )
        return summarize_job(job)
    finally:
        # Never return (including on cancellation) while the native worker can
        # still own an OpenHands process or Docker sandbox.
        worker_pool.shutdown(wait=True, cancel_futures=True)
        # The HUD observer flushes before taskset.run returns. Cleanup here
        # cannot erase the model's final workspace before telemetry captures it.
        if cleanup_after_rollout:
            cleanup_tracked_root(rollout_config.tmp_dir)


async def run_many(
    task_ids: Iterable[str],
    config: NativeOpenHandsConfig,
    *,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    executor: Callable[[NativeOpenHandsConfig, NativeTaskBinding], NativeReceipt] | None = None,
    uuid_factory: Callable[[], UUID] = uuid4,
    job_name: str | None = None,
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
    worker_pool = ThreadPoolExecutor(
        max_workers=max_concurrent,
        thread_name_prefix="cybergym-native",
    )
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

        job = await _start_named_job(taskset, job_name)
        job = await taskset.run(
            NativeOpenHandsBatchAgent(
                rollout_configs,
                executor=executor,
                worker_pool=worker_pool,
            ),
            runtime=LocalRuntime(environment_for),
            # This is the only concurrency controller: HUD releases the next
            # waiting rollout as soon as any active slot completes.
            max_concurrent=max_concurrent,
            job=job,
        )
        return summarize_batch_job(job)
    finally:
        # The dedicated pool both makes width 15 independent of asyncio's
        # host-sized default executor and keeps cancellation from orphaning a
        # native lifecycle after its HUD slot appears free.
        worker_pool.shutdown(wait=True, cancel_futures=True)
        # Normal cleanup happens in each LocalRuntime shutdown, after that
        # rollout's file-tracking observer flush. This is only a failure or
        # cancellation fallback for roots whose runtime never shut down.
        for task_id, rollout_config in rollout_configs.items():
            if cleanup_roots[task_id]:
                cleanup_tracked_root(rollout_config.tmp_dir)


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
    parser.add_argument(
        "--reasoning-effort",
        choices=("xhigh",),
        help="GPT-5.6 Sol reasoning effort (integration transport compatibility)",
    )
    parser.add_argument(
        "--job-name",
        help="human-facing HUD Job name (default: HUD's task-derived name)",
    )
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--tmp-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument(
        "--grader-server-mode",
        choices=("images", "binary"),
        default="images",
        help="non-secret upstream server runtime profile recorded in the HUD receipt",
    )
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
        reasoning_effort=args.reasoning_effort,
        log_dir=args.log_dir,
        tmp_dir=args.tmp_dir,
        max_iter=args.max_iter,
        timeout=args.timeout,
        base_url=args.base_url,
        grader_server_mode=args.grader_server_mode,
        top_p=args.top_p,
        temperature=args.temperature,
        max_output_tokens=args.max_output_tokens,
        seed=args.seed,
        silent=args.silent,
        remove_tmp=not args.keep_tmp,
        debug=args.debug,
    )
    if len(selected) == 1:
        result = asyncio.run(run_one(selected[0], config, job_name=args.job_name))
    else:
        result = asyncio.run(
            run_many(
                selected,
                config,
                max_concurrent=args.max_concurrent,
                job_name=args.job_name,
            )
        )
    result = asyncio.run(verify_and_persist_remote_receipt(result, results_dir=args.log_dir.parent))
    print(json.dumps(result, indent=2, default=str))
    if result["is_error"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

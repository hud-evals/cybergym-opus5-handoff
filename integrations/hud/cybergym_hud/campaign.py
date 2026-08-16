"""Durable, spend-gated orchestration for the full CyberGym catalog."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import threading
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from hud.eval import Job
from hud.settings import settings
from hud.utils.platform import PlatformClient

from .contract import CONTRACT, validate_contract
from .native import (
    CAMPAIGN_RUNTIME_MEMORY_BYTES,
    CAMPAIGN_RUNTIME_MEMORY_SWAP_BYTES,
    CAMPAIGN_RUNTIME_NANO_CPUS,
    NativeOpenHandsConfig,
    execute_upstream_openhands,
)
from .openhands_trace import (
    TraceImportError,
    validate_remote_evaluation_receipt,
    validate_remote_trace_projection,
)
from .receipt import NativeReceipt, NativeTaskBinding
from .runtime_network import (
    RUNTIME_NETWORK_NAME,
    expected_network_attestation,
    network_attestation_sha256,
)
from .scheduler import run_many, verify_and_persist_remote_receipt, write_summary
from .taskset import make_taskset
from .taskset import task_ids as catalog_task_ids

CAMPAIGN_SCHEMA_VERSION = "1"
CAMPAIGN_JOB_NAME = "cybergym-gpt5.6-sol-no-internet-v1"
CAMPAIGN_MODEL = "gpt-5.6-sol"
CAMPAIGN_REASONING_EFFORT = "xhigh"
CAMPAIGN_MAX_ITER = 200
CAMPAIGN_TIMEOUT_SECONDS = 3600
CAMPAIGN_MAX_CONCURRENT = 6
DEFAULT_SHARD_SIZE = 12
MAX_SHARD_SIZE = 24
TERMINAL_STATUSES = frozenset({"completed", "error", "cancelled"})


class CampaignBlocked(RuntimeError):
    """Raised when continuing could repeat spend or amplify infrastructure failure."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _catalog_digest(task_ids: Iterable[str]) -> str:
    encoded = "\n".join(task_ids).encode()
    return hashlib.sha256(encoded).hexdigest()


def _endpoint_digest(value: str) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest() if value else None


def _task_slug(task_id: str) -> str:
    return task_id.replace(":", "-")


def _uuid_key(value: object) -> str:
    rendered = str(value)
    try:
        return UUID(rendered).hex
    except ValueError:
        return rendered


def _campaign_identity(
    config: NativeOpenHandsConfig,
    task_ids: tuple[str, ...],
    shard_size: int,
    *,
    artifact_fingerprints: dict[str, str] | None = None,
) -> dict[str, Any]:
    profile = config.receipt_profile().model_dump(mode="json")
    return {
        "benchmark": "cybergym-og-native-level1",
        "benchmark_commit": CONTRACT["benchmark"]["commit"],
        "agent_commit": CONTRACT["agent_scaffold"]["gitlink_commit"],
        "catalog_sha256": _catalog_digest(task_ids),
        "task_count": len(task_ids),
        "repository_root": str(config.repository_root),
        "data_dir": str(config.data_dir),
        "server": config.server,
        "job_name": CAMPAIGN_JOB_NAME,
        "run_profile": profile,
        # Do not retain a custom endpoint that could contain a path token.
        "model_endpoint_sha256": _endpoint_digest(config.base_url),
        "artifact_fingerprints": artifact_fingerprints or {},
        "shard_size": shard_size,
    }


def validate_campaign_profile(config: NativeOpenHandsConfig, *, max_concurrent: int, shard_size: int) -> None:
    """Fail closed on accidental benchmark/model/budget drift."""

    config = config.normalized()
    if config.llm_api_key is not None:
        raise ValueError("paid campaign requires provider credentials through the environment, never config/argv")
    expected = {
        "model": CAMPAIGN_MODEL,
        "reasoning_effort": CAMPAIGN_REASONING_EFFORT,
        "max_iter": CAMPAIGN_MAX_ITER,
        "timeout": CAMPAIGN_TIMEOUT_SECONDS,
        "top_p": 1.0,
        "temperature": 0.0,
        "max_output_tokens": 2048,
        "seed": None,
        "native_tool_calling": None,
        "silent": True,
        "base_url": "",
        "runtime_nano_cpus": CAMPAIGN_RUNTIME_NANO_CPUS,
        "runtime_memory_bytes": CAMPAIGN_RUNTIME_MEMORY_BYTES,
        "runtime_memory_swap_bytes": CAMPAIGN_RUNTIME_MEMORY_SWAP_BYTES,
        "runtime_network": RUNTIME_NETWORK_NAME,
    }
    drift = {
        name: {"expected": value, "observed": getattr(config, name)}
        for name, value in expected.items()
        if getattr(config, name) != value
    }
    if drift:
        raise ValueError(f"CyberGym paid campaign profile drift: {drift}")
    if not 1 <= max_concurrent <= CAMPAIGN_MAX_CONCURRENT:
        raise ValueError(f"max_concurrent must be between 1 and {CAMPAIGN_MAX_CONCURRENT}")
    if not 1 <= shard_size <= MAX_SHARD_SIZE:
        raise ValueError(f"shard_size must be between 1 and {MAX_SHARD_SIZE}")


def load_preflight_fingerprints(
    path: Path,
    *,
    config: NativeOpenHandsConfig,
    max_concurrent: int,
) -> dict[str, str]:
    """Bind a matching no-spend full-corpus report to the paid campaign."""

    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise CampaignBlocked(f"full-corpus preflight report must be a non-symlink mode-0600 file: {path}")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignBlocked(f"full-corpus preflight report is unreadable: {path}") from exc
    catalog = catalog_task_ids(config.repository_root)
    required = {
        "schema_version": "1",
        "no_model_call": True,
        "catalog_sha256": _catalog_digest(catalog),
        "task_count": len(catalog),
        "grader_server_mode": config.grader_server_mode,
        "max_concurrent": max_concurrent,
        "runtime_limits": {
            "nano_cpus": CAMPAIGN_RUNTIME_NANO_CPUS,
            "memory": CAMPAIGN_RUNTIME_MEMORY_BYTES,
            "memory_swap": CAMPAIGN_RUNTIME_MEMORY_SWAP_BYTES,
        },
        "runtime_network": expected_network_attestation(server_url=config.server),
    }
    drift = {
        key: {"expected": value, "observed": report.get(key)}
        for key, value in required.items()
        if report.get(key) != value
    }
    if drift:
        raise CampaignBlocked(f"full-corpus preflight report does not match this campaign: {drift}")
    fingerprints: dict[str, str] = {}
    fingerprint_keys = [
        "source_artifact_sha256",
        "grader_artifact_sha256",
        "source_provenance_sha256",
        "source_selected_manifest_sha256",
        "runtime_network_sha256",
    ]
    if config.grader_server_mode == "binary":
        fingerprint_keys.append("binary_tree_sha256")
    for key in fingerprint_keys:
        value = report.get(key)
        malformed = (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        )
        if malformed:
            raise CampaignBlocked(f"full-corpus preflight report has no valid {key}")
        fingerprints[key] = value
    expected_network_sha256 = network_attestation_sha256(required["runtime_network"])
    if fingerprints["runtime_network_sha256"] != expected_network_sha256:
        raise CampaignBlocked("full-corpus preflight runtime-network fingerprint drifted")
    return fingerprints


@contextmanager
def campaign_lock(state_dir: Path):
    """Exclude a second operator process without placing secrets in a PID command."""

    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state_dir.is_symlink():
        raise CampaignBlocked(f"campaign state directory may not be a symlink: {state_dir}")
    os.chmod(state_dir, 0o700)
    lock_path = state_dir / "campaign.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CampaignBlocked(f"another campaign operator owns {lock_path}") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class CampaignState:
    """Thread-safe, atomic campaign journal written before every native call."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / "manifest.json"
        self.shard_summary_dir = state_dir / "shards"
        self._lock = threading.Lock()
        self.payload: dict[str, Any] = {}

    def initialize(
        self,
        *,
        identity: dict[str, Any],
        task_ids: tuple[str, ...],
        shard_size: int,
    ) -> None:
        self.shard_summary_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.shard_summary_dir.is_symlink():
            raise CampaignBlocked(f"campaign shard directory may not be a symlink: {self.shard_summary_dir}")
        os.chmod(self.shard_summary_dir, 0o700)
        if self.path.exists():
            if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
                raise CampaignBlocked(f"campaign manifest must be a non-symlink mode-0600 file: {self.path}")
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CampaignBlocked(f"campaign manifest is unreadable: {self.path}") from exc
            if payload.get("schema_version") != CAMPAIGN_SCHEMA_VERSION:
                raise CampaignBlocked("campaign manifest schema does not match this operator")
            if payload.get("identity") != identity:
                raise CampaignBlocked("campaign identity/profile differs from the existing manifest")
            self.payload = payload
            self._validate_loaded(task_ids=task_ids, shard_size=shard_size)
            return

        shards = []
        for index, offset in enumerate(range(0, len(task_ids), shard_size)):
            shards.append(
                {
                    "index": index,
                    "task_ids": list(task_ids[offset : offset + shard_size]),
                    "completed_task_ids": [],
                    "status": "pending",
                    "attempts": [],
                }
            )
        self.payload = {
            "schema_version": CAMPAIGN_SCHEMA_VERSION,
            "created_at": _now(),
            "updated_at": _now(),
            "identity": identity,
            "halt": None,
            "shards": shards,
        }
        self._save()

    def _validate_loaded(self, *, task_ids: tuple[str, ...], shard_size: int) -> None:
        expected_shards = [
            list(task_ids[offset : offset + shard_size]) for offset in range(0, len(task_ids), shard_size)
        ]
        shards = self.payload.get("shards")
        if not isinstance(shards, list) or len(shards) != len(expected_shards):
            raise CampaignBlocked("campaign manifest shard count drifted")
        for index, (shard, expected) in enumerate(zip(shards, expected_shards, strict=True)):
            if not isinstance(shard, dict) or shard.get("index") != index or shard.get("task_ids") != expected:
                raise CampaignBlocked(f"campaign manifest shard {index} no longer matches the deterministic plan")
            completed = shard.get("completed_task_ids")
            attempts = shard.get("attempts")
            if (
                not isinstance(completed, list)
                or len(completed) != len(set(completed))
                or not set(completed).issubset(expected)
                or not isinstance(attempts, list)
            ):
                raise CampaignBlocked(f"campaign manifest shard {index} completion journal is malformed")
            witnessed: set[str] = set()
            for attempt_index, attempt in enumerate(attempts, start=1):
                if not isinstance(attempt, dict) or attempt.get("number") != attempt_index:
                    raise CampaignBlocked(f"campaign manifest shard {index} attempt ordering drifted")
                attempt_tasks = attempt.get("task_ids")
                launched = attempt.get("launched_task_ids")
                returned = attempt.get("native_returned_task_ids")
                if (
                    not isinstance(attempt_tasks, list)
                    or len(attempt_tasks) != len(set(attempt_tasks))
                    or not set(attempt_tasks).issubset(expected)
                    or not isinstance(launched, list)
                    or not set(launched).issubset(attempt_tasks)
                    or not isinstance(returned, list)
                    or not set(returned).issubset(launched)
                    or attempt.get("job_name") != CAMPAIGN_JOB_NAME
                    or not attempt.get("job_id")
                ):
                    raise CampaignBlocked(f"campaign manifest shard {index} attempt {attempt_index} is malformed")
                if attempt.get("status") in {"verified", "recovered"}:
                    summary_path = Path(str(attempt.get("summary_path") or ""))
                    try:
                        inside_summary_dir = summary_path.resolve().parent == self.shard_summary_dir.resolve()
                    except OSError:
                        inside_summary_dir = False
                    if (
                        not inside_summary_dir
                        or not summary_path.is_file()
                        or summary_path.is_symlink()
                        or summary_path.stat().st_mode & 0o077
                    ):
                        raise CampaignBlocked(
                            f"campaign manifest shard {index} attempt {attempt_index} lost its mode-0600 summary"
                        )
                    try:
                        summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise CampaignBlocked(
                            f"campaign manifest shard {index} attempt {attempt_index} summary is unreadable"
                        ) from exc
                    if not (
                        summary.get("hud_remote_receipt_verified") is True
                        and summary.get("hud_remote_events_verified") is True
                    ):
                        raise CampaignBlocked(
                            f"campaign manifest shard {index} attempt {attempt_index} summary is not HUD-verified"
                        )
                    witnessed.update(attempt_tasks if attempt["status"] == "verified" else summary.get("task_ids", []))
            if not set(completed).issubset(witnessed):
                raise CampaignBlocked(f"campaign manifest shard {index} marks tasks complete without verified evidence")

    def _save(self) -> None:
        self.payload["updated_at"] = _now()
        write_summary(self.path, self.payload)

    def save(self) -> None:
        with self._lock:
            self._save()

    def shard(self, index: int) -> dict[str, Any]:
        return self.payload["shards"][index]

    def pending_task_ids(self, index: int) -> tuple[str, ...]:
        shard = self.shard(index)
        completed = set(shard["completed_task_ids"])
        return tuple(task_id for task_id in shard["task_ids"] if task_id not in completed)

    def start_attempt(self, index: int, *, job: Job, task_ids: tuple[str, ...], max_concurrent: int) -> int:
        with self._lock:
            shard = self.shard(index)
            attempt_number = len(shard["attempts"]) + 1
            shard["attempts"].append(
                {
                    "number": attempt_number,
                    "job_id": job.id,
                    "job_name": job.name,
                    "task_ids": list(task_ids),
                    "max_concurrent": max_concurrent,
                    "status": "running",
                    "started_at": _now(),
                    "launched_task_ids": [],
                    "native_returned_task_ids": [],
                    "summary_path": None,
                }
            )
            shard["status"] = "running"
            self._save()
            return attempt_number

    def _attempt(self, index: int, attempt_number: int) -> dict[str, Any]:
        attempt = self.shard(index)["attempts"][attempt_number - 1]
        if attempt["number"] != attempt_number:
            raise CampaignBlocked("campaign attempt journal ordering drifted")
        return attempt

    def mark_launched(self, index: int, attempt_number: int, task_id: str) -> None:
        with self._lock:
            attempt = self._attempt(index, attempt_number)
            if attempt["status"] != "running" or task_id not in attempt["task_ids"]:
                raise CampaignBlocked(f"refusing unjournaled paid rollout: {task_id}")
            if task_id in attempt["launched_task_ids"]:
                raise CampaignBlocked(f"refusing duplicate paid rollout within one attempt: {task_id}")
            attempt["launched_task_ids"].append(task_id)
            self._save()  # Durable before execute_upstream_openhands can call the model.

    def mark_native_returned(self, index: int, attempt_number: int, task_id: str) -> None:
        with self._lock:
            attempt = self._attempt(index, attempt_number)
            if task_id not in attempt["native_returned_task_ids"]:
                attempt["native_returned_task_ids"].append(task_id)
                self._save()

    def complete_attempt(
        self,
        index: int,
        attempt_number: int,
        *,
        completed_task_ids: Iterable[str],
        status: str,
        summary_path: Path,
        has_errors: bool,
    ) -> None:
        with self._lock:
            shard = self.shard(index)
            attempt = self._attempt(index, attempt_number)
            completed = set(shard["completed_task_ids"])
            completed.update(completed_task_ids)
            shard["completed_task_ids"] = [task_id for task_id in shard["task_ids"] if task_id in completed]
            attempt.update({"status": status, "finished_at": _now(), "summary_path": str(summary_path)})
            if len(shard["completed_task_ids"]) == len(shard["task_ids"]):
                shard["status"] = "verified_with_errors" if has_errors else "verified"
            else:
                shard["status"] = "pending"
            if has_errors:
                self.payload["halt"] = {
                    "reason": "verified shard contains infrastructure-error or cancelled traces",
                    "shard_index": index,
                    "job_id": attempt["job_id"],
                    "created_at": _now(),
                }
            self._save()

    def record_unresolved(self, index: int, attempt_number: int, task_ids: Iterable[str]) -> None:
        with self._lock:
            unresolved = list(task_ids)
            attempt = self._attempt(index, attempt_number)
            attempt["status"] = "reconciliation_required"
            attempt["unresolved_task_ids"] = unresolved
            self.shard(index)["status"] = "blocked"
            self._save()

    def acknowledge_halt(self) -> None:
        with self._lock:
            if self.payload.get("halt") is not None:
                self.payload.setdefault("halt_acknowledgements", []).append(
                    {"halt": self.payload["halt"], "acknowledged_at": _now()}
                )
                self.payload["halt"] = None
                self._save()


async def fetch_job_traces(job_id: str, *, client: PlatformClient | None = None) -> list[dict[str, Any]]:
    """Fetch every trace row for a small campaign shard."""

    client = client or PlatformClient.from_settings()
    rows: list[dict[str, Any]] = []
    limit = 1000
    for offset in range(0, 100_000, limit):
        data = await client.aget(f"/jobs/{job_id}/traces", params={"limit": limit, "offset": offset})
        page = data if isinstance(data, list) else data.get("items", [])
        if not isinstance(page, list):
            raise CampaignBlocked(f"HUD returned a malformed trace page for job {job_id}")
        rows.extend(row for row in page if isinstance(row, dict))
        if len(page) < limit:
            return rows
    raise CampaignBlocked(f"HUD trace pagination did not terminate for job {job_id}")


async def require_remote_job_receipt(job: Job, *, client: PlatformClient | None = None) -> None:
    """Prove HUD accepted the named Job before any provider call can start."""

    client = client or PlatformClient.from_settings()
    last_problem = "no response"
    for attempt in range(3):
        try:
            remote = await client.aget(f"/jobs/{job.id}")
        except Exception as exc:  # HUD's Job.start reporter is intentionally best-effort.
            last_problem = f"{type(exc).__name__}: {exc}"
        else:
            if (
                isinstance(remote, dict)
                and _uuid_key(remote.get("id")) == _uuid_key(job.id)
                and remote.get("name") == CAMPAIGN_JOB_NAME
                and remote.get("can_edit") is True
                and remote.get("group_size") == 1
                and remote.get("taskset_id") is None
            ):
                traces = await client.aget(f"/jobs/{job.id}/traces", params={"limit": 1, "offset": 0})
                page = traces if isinstance(traces, list) else traces.get("items", [])
                if isinstance(page, list) and not page:
                    return
                last_problem = "HUD acknowledged the Job with unexpected pre-existing traces"
                continue
            last_problem = "HUD returned a mismatched or non-editable Job receipt"
        if attempt < 2:
            await asyncio.sleep(1.0)
    raise CampaignBlocked(f"HUD did not acknowledge named Job {job.id} before the paid boundary: {last_problem}")


async def require_remote_trace_events(
    trace_rows: Iterable[dict[str, Any]],
    *,
    client: PlatformClient | None = None,
) -> set[str]:
    """Require telemetry events for an already-validated subset of job rows."""

    client = client or PlatformClient.from_settings()
    missing: list[str] = []
    grader_errors: set[str] = set()
    for row in trace_rows:
        trace_id = str(row["id"])
        observed = False
        last_problem = "no events"
        for attempt in range(31):
            data = await client.aget(f"/trace/{trace_id}/events")
            events = data.get("events", []) if isinstance(data, dict) else []
            if isinstance(events, list):
                try:
                    validate_remote_trace_projection(events)
                    if validate_remote_evaluation_receipt(events, expected_reward=row.get("reward")):
                        grader_errors.add(_uuid_key(trace_id))
                    observed = True
                    break
                except TraceImportError as exc:
                    last_problem = str(exc)
            if attempt < 30:
                await asyncio.sleep(2.0)
        if not observed:
            missing.append(f"{trace_id} ({last_problem})")
    if missing:
        raise CampaignBlocked("HUD terminal receipts lack remotely readable telemetry events: " + ", ".join(missing))
    return grader_errors


async def require_remote_trace_enter(
    job_id: str,
    trace_id: str,
    task_id: str,
    *,
    client: PlatformClient | None = None,
) -> None:
    """Prove the asynchronous HUD trace-enter row before crossing spend."""

    client = client or PlatformClient.from_settings()
    expected_trace = _uuid_key(trace_id)
    expected_slug = _task_slug(task_id)
    last_problem = "trace row was absent"
    for attempt in range(5):
        rows = await fetch_job_traces(job_id, client=client)
        matches = [row for row in rows if _uuid_key(row.get("id")) == expected_trace]
        if len(matches) == 1 and matches[0].get("task_slug") == expected_slug:
            return
        if matches:
            last_problem = "trace row had a mismatched task slug"
        if attempt < 4:
            await asyncio.sleep(1.0)
    raise CampaignBlocked(
        f"HUD did not acknowledge trace {trace_id} as {expected_slug} before the paid boundary: {last_problem}"
    )


async def reconcile_running_attempt(
    state: CampaignState,
    shard_index: int,
    attempt: dict[str, Any],
    *,
    client: PlatformClient | None = None,
) -> None:
    """Recover terminal remote rows and never auto-repeat an uncertain paid call."""

    rows = await fetch_job_traces(attempt["job_id"], client=client)
    expected_by_slug = {_task_slug(task_id): task_id for task_id in attempt["task_ids"]}
    by_task: dict[str, list[dict[str, Any]]] = {}
    unexpected: list[str] = []
    for row in rows:
        slug = row.get("task_slug")
        task_id = expected_by_slug.get(slug)
        if task_id is None:
            unexpected.append(str(slug))
        else:
            by_task.setdefault(task_id, []).append(row)
    duplicates = [task_id for task_id, task_rows in by_task.items() if len(task_rows) != 1]
    if unexpected or duplicates:
        raise CampaignBlocked(
            f"HUD job/task mapping drift for {attempt['job_id']}: unexpected={unexpected}, duplicates={duplicates}"
        )

    terminal: list[str] = []
    terminal_rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    launched = set(attempt.get("launched_task_ids", []))
    for task_id in attempt["task_ids"]:
        task_rows = by_task.get(task_id, [])
        if task_rows and task_id in launched:
            row = task_rows[0]
            if row.get("status") in TERMINAL_STATUSES and row.get("reward") is not None:
                terminal.append(task_id)
                terminal_rows.append(row)
            else:
                unresolved.append(task_id)
        elif task_id in launched:
            unresolved.append(task_id)
        # A task with no remote row and no durable launch marker never reached
        # execute_upstream_openhands and is safe to place in a later attempt.

    grader_error_ids = await require_remote_trace_events(terminal_rows, client=client) if terminal_rows else set()
    if unresolved:
        state.record_unresolved(shard_index, attempt["number"], unresolved)
        raise CampaignBlocked(
            "campaign restart found potentially paid tasks without terminal, rewarded HUD receipts; "
            f"job={attempt['job_id']} tasks={unresolved}. No task was relaunched."
        )

    web = settings.hud_web_url.rstrip("/")
    recovered = {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "recovered_after_restart": True,
        "shard_index": shard_index,
        "attempt_number": attempt["number"],
        "job_id": attempt["job_id"],
        "job_name": attempt["job_name"],
        "job_url": f"{web}/jobs/{attempt['job_id']}",
        "task_ids": terminal,
        "trace_ids": [str(row["id"]) for row in terminal_rows],
        "trace_urls": [f"{web}/trace/{row['id']}" for row in terminal_rows],
        "runs": terminal_rows,
        "hud_remote_receipt_verified": True,
        "hud_remote_events_verified": True,
    }
    summary_path = state.shard_summary_dir / (
        f"shard-{shard_index + 1:04d}-attempt-{attempt['number']:02d}-{attempt['job_id']}-recovered.json"
    )
    write_summary(summary_path, recovered)
    has_errors = any(
        row.get("status") in {"error", "cancelled"} or _uuid_key(row["id"]) in grader_error_ids for row in terminal_rows
    )
    state.complete_attempt(
        shard_index,
        attempt["number"],
        completed_task_ids=terminal,
        status="recovered",
        summary_path=summary_path,
        has_errors=has_errors,
    )


async def reconcile_campaign(state: CampaignState, *, client: PlatformClient | None = None) -> None:
    for shard in state.payload["shards"]:
        for attempt in shard["attempts"]:
            if attempt["status"] in {"running", "reconciliation_required"}:
                await reconcile_running_attempt(state, shard["index"], attempt, client=client)


def validate_attempt_result(
    result: dict[str, Any],
    *,
    expected_task_ids: tuple[str, ...],
    config: NativeOpenHandsConfig,
    job: Job,
) -> None:
    """Prove the local batch result covers every selected row exactly once."""

    if str(result.get("job_id")) != str(job.id) or result.get("job_name") != CAMPAIGN_JOB_NAME:
        raise CampaignBlocked("batch result is not attached to the pre-journaled named HUD Job")
    runs = result.get("runs")
    if not isinstance(runs, list) or len(runs) != len(expected_task_ids):
        raise CampaignBlocked(
            f"HUD batch returned {len(runs) if isinstance(runs, list) else 'malformed'} runs "
            f"for {len(expected_task_ids)} selected tasks"
        )
    observed: list[str] = []
    expected_profile = config.receipt_profile().model_dump(mode="json")
    for run in runs:
        if not isinstance(run, dict) or not run.get("trace_id"):
            raise CampaignBlocked("HUD batch returned a run without a trace ID")
        receipt = run.get("native_receipt")
        if not isinstance(receipt, dict):
            raise CampaignBlocked("HUD batch returned a run without a typed native receipt")
        task_id = receipt.get("task_id")
        if not isinstance(task_id, str):
            raise CampaignBlocked("HUD native receipt omitted its task ID")
        if receipt.get("server") != config.server or receipt.get("run_profile") != expected_profile:
            raise CampaignBlocked(f"HUD native receipt profile drifted for {task_id}")
        imported = run.get("openhands_trace_import")
        expected_import_status = "completed" if receipt.get("status") == "completed" else "partial_error"
        if not isinstance(imported, dict) or imported.get("status") != expected_import_status:
            raise CampaignBlocked(f"HUD OpenHands transcript import failed for {task_id}")
        digest = imported.get("projected_steps_sha256")
        counts = (
            imported.get("projected_step_count"),
            imported.get("agent_step_count"),
            imported.get("tool_step_count"),
            imported.get("user_step_count", 0),
        )
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not all(isinstance(value, int) and value >= 0 for value in counts)
            or counts[0] != counts[1] + counts[2] + counts[3]
            or counts[1] < 1
        ):
            raise CampaignBlocked(f"HUD OpenHands transcript import receipt is invalid for {task_id}")
        observed.append(task_id)
    if len(observed) != len(set(observed)) or set(observed) != set(expected_task_ids):
        raise CampaignBlocked(
            f"HUD batch task coverage drifted: expected={list(expected_task_ids)}, observed={observed}"
        )
    if result.get("task_count") != len(expected_task_ids):
        raise CampaignBlocked("HUD batch aggregate task count does not match its native receipts")


async def run_campaign(
    config: NativeOpenHandsConfig,
    *,
    state_dir: Path,
    max_concurrent: int,
    shard_size: int = DEFAULT_SHARD_SIZE,
    confirm_paid_all: bool,
    continue_after_errors: bool = False,
    client: PlatformClient | None = None,
    job_factory: Callable[..., Any] = Job.start,
    job_receipt_verifier: Callable[..., Any] = require_remote_job_receipt,
    batch_runner: Callable[..., Any] = run_many,
    receipt_verifier: Callable[..., Any] = verify_and_persist_remote_receipt,
    native_executor: Callable[[NativeOpenHandsConfig, NativeTaskBinding], NativeReceipt] = execute_upstream_openhands,
    artifact_fingerprints: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run deterministic shards, checkpointing before each potentially paid call."""

    if not confirm_paid_all:
        raise ValueError("the complete paid catalog requires explicit --confirm-paid-all")
    if artifact_fingerprints is None:
        raise ValueError("paid campaign requires full-corpus source and grader fingerprints")
    config = config.normalized()
    validate_campaign_profile(config, max_concurrent=max_concurrent, shard_size=shard_size)
    validate_contract(root=config.repository_root)
    task_ids = catalog_task_ids(config.repository_root)
    identity = _campaign_identity(
        config,
        task_ids,
        shard_size,
        artifact_fingerprints=artifact_fingerprints,
    )

    state = CampaignState(state_dir)
    state.initialize(identity=identity, task_ids=task_ids, shard_size=shard_size)
    await reconcile_campaign(state, client=client)
    if state.payload.get("halt") is not None:
        if not continue_after_errors:
            halt = state.payload["halt"]
            raise CampaignBlocked(
                f"campaign halted after verified infrastructure errors in job {halt['job_id']}; "
                "inspect its shard summary, then pass --continue-after-errors to skip those paid tasks"
            )
        state.acknowledge_halt()

    for shard in state.payload["shards"]:
        pending = state.pending_task_ids(shard["index"])
        if not pending:
            continue
        taskset = make_taskset(server=config.server, selected=pending, root=config.repository_root)
        job = await job_factory(CAMPAIGN_JOB_NAME, taskset_id=taskset.api_id)
        if job.name != CAMPAIGN_JOB_NAME:
            raise CampaignBlocked("HUD Job factory changed the required campaign job name")
        await job_receipt_verifier(job, client=client)
        attempt_number = state.start_attempt(shard["index"], job=job, task_ids=pending, max_concurrent=max_concurrent)
        shard_index = shard["index"]
        current_attempt = attempt_number

        def journaled_executor(
            rollout_config: NativeOpenHandsConfig,
            binding: NativeTaskBinding,
            *,
            _shard_index: int = shard_index,
            _attempt: int = current_attempt,
        ) -> NativeReceipt:
            state.mark_launched(_shard_index, _attempt, binding.task_id)
            receipt = native_executor(rollout_config, binding)
            state.mark_native_returned(_shard_index, _attempt, binding.task_id)
            return receipt

        async def prelaunch_verifier(
            trace_id: str,
            task_id: str,
            *,
            _job_id: str = str(job.id),
        ) -> None:
            await require_remote_trace_enter(_job_id, trace_id, task_id, client=client)

        result = await batch_runner(
            pending,
            config,
            max_concurrent=max_concurrent,
            executor=journaled_executor,
            job_name=CAMPAIGN_JOB_NAME,
            job=job,
            prelaunch_verifier=prelaunch_verifier,
        )
        validate_attempt_result(result, expected_task_ids=pending, config=config, job=job)
        verified = await receipt_verifier(result, results_dir=config.log_dir.parent)
        summary_path = state.shard_summary_dir / (
            f"shard-{shard['index'] + 1:04d}-attempt-{attempt_number:02d}-{job.id}.json"
        )
        shard_summary = {
            "schema_version": CAMPAIGN_SCHEMA_VERSION,
            "shard_index": shard["index"],
            "attempt_number": attempt_number,
            "task_ids": list(pending),
            **verified,
        }
        write_summary(summary_path, shard_summary)
        state.complete_attempt(
            shard["index"],
            attempt_number,
            completed_task_ids=pending,
            status="verified",
            summary_path=summary_path,
            has_errors=bool(verified["is_error"]),
        )
        if verified["is_error"]:
            raise CampaignBlocked(
                f"verified shard job {job.id} contains infrastructure errors; no later shard was started"
            )

    total = len(task_ids)
    completed = sum(len(shard["completed_task_ids"]) for shard in state.payload["shards"])
    summary = {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "job_name": CAMPAIGN_JOB_NAME,
        "task_count": total,
        "completed_task_count": completed,
        "complete": completed == total,
        "max_iter": CAMPAIGN_MAX_ITER,
        "timeout_seconds": CAMPAIGN_TIMEOUT_SECONDS,
        "max_concurrent": max_concurrent,
        "shard_size": shard_size,
        "manifest_path": str(state.path),
        "jobs": [
            {
                "shard_index": shard["index"],
                "job_id": attempt["job_id"],
                "job_name": attempt["job_name"],
                "status": attempt["status"],
                "summary_path": attempt.get("summary_path"),
            }
            for shard in state.payload["shards"]
            for attempt in shard["attempts"]
        ],
    }
    write_summary(state_dir / "campaign-summary.json", summary)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a restart-safe paid CyberGym full-catalog campaign")
    parser.add_argument("--all", action="store_true", help="required full-catalog selection acknowledgement")
    parser.add_argument("--confirm-paid-all", action="store_true", help="required provider-spend acknowledgement")
    parser.add_argument("--continue-after-errors", action="store_true")
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--grader-server-mode", choices=("images", "binary"), default="images")
    parser.add_argument("--max-concurrent", type=int, default=1)
    parser.add_argument("--shard-size", type=int, default=DEFAULT_SHARD_SIZE)
    parser.add_argument("--keep-tmp", action="store_true")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if not args.all:
        parser.error("the campaign runner requires --all")
    if not args.confirm_paid_all:
        parser.error("the complete paid catalog requires --confirm-paid-all")
    results_dir = args.results_dir.expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    config = NativeOpenHandsConfig(
        repository_root=args.repository_root,
        data_dir=args.data_dir,
        server=args.server,
        model=CAMPAIGN_MODEL,
        reasoning_effort=CAMPAIGN_REASONING_EFFORT,
        log_dir=results_dir / "logs",
        tmp_dir=results_dir / "tmp",
        max_iter=CAMPAIGN_MAX_ITER,
        timeout=CAMPAIGN_TIMEOUT_SECONDS,
        base_url=os.environ.get("CG_MODEL_BASE_URL", ""),
        grader_server_mode=args.grader_server_mode,
        # A multi-day service must not copy model/tool output into the system
        # journal. The private per-rollout OpenHands logs remain available.
        silent=True,
        remove_tmp=not args.keep_tmp,
        runtime_nano_cpus=CAMPAIGN_RUNTIME_NANO_CPUS,
        runtime_memory_bytes=CAMPAIGN_RUNTIME_MEMORY_BYTES,
        runtime_memory_swap_bytes=CAMPAIGN_RUNTIME_MEMORY_SWAP_BYTES,
        runtime_network=RUNTIME_NETWORK_NAME,
    )
    config.log_dir.mkdir(parents=True, exist_ok=True)
    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    state_dir = results_dir / "campaign-gpt56-sol-200-no-internet-v1"
    try:
        with campaign_lock(state_dir):
            artifact_fingerprints = load_preflight_fingerprints(
                state_dir / "full-corpus-preflight.json",
                config=config,
                max_concurrent=args.max_concurrent,
            )
            summary = asyncio.run(
                run_campaign(
                    config,
                    state_dir=state_dir,
                    max_concurrent=args.max_concurrent,
                    shard_size=args.shard_size,
                    confirm_paid_all=args.confirm_paid_all,
                    continue_after_errors=args.continue_after_errors,
                    artifact_fingerprints=artifact_fingerprints,
                )
            )
    except CampaignBlocked as exc:
        parser.exit(3, f"campaign blocked: {exc}\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

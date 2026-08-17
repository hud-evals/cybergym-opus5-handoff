"""Restart-safe noncanonical CyberGym campaign on private Daytona sandboxes."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

from .campaign import (
    CampaignBlocked,
    _catalog_digest,
    campaign_lock,
    load_preflight_fingerprints,
    run_campaign,
)
from .daytona_lane import (
    DAYTONA_IMAGE,
    reconcile_daytona_sandboxes,
    validate_daytona_contract,
)
from .native import (
    CAMPAIGN_RUNTIME_MEMORY_BYTES,
    CAMPAIGN_RUNTIME_MEMORY_SWAP_BYTES,
    CAMPAIGN_RUNTIME_NANO_CPUS,
    NativeOpenHandsConfig,
)
from .taskset import task_ids as catalog_task_ids

JOB_NAME = "cybergym-gpt5.6-sol-2"
MAX_PRIVATE_FILE_BYTES = 16 * 1024 * 1024


def _read_private(path: Path) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 or before.st_size > MAX_PRIVATE_FILE_BYTES:
        raise CampaignBlocked(f"required campaign input is not a private bounded regular file: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
            raise CampaignBlocked(f"campaign input changed while opening: {path}")
        encoded = b""
        while len(encoded) <= MAX_PRIVATE_FILE_BYTES:
            block = os.read(descriptor, min(1024 * 1024, MAX_PRIVATE_FILE_BYTES + 1 - len(encoded)))
            if not block:
                break
            encoded += block
        if len(encoded) > MAX_PRIVATE_FILE_BYTES:
            raise CampaignBlocked(f"campaign input exceeds its byte limit: {path}")
    finally:
        os.close(descriptor)
    after = path.lstat()
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ):
        raise CampaignBlocked(f"campaign input changed while reading: {path}")
    return encoded


def _read_private_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_read_private(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignBlocked(f"campaign JSON input is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise CampaignBlocked(f"campaign JSON input is not an object: {path}")
    return payload


def _load_task_file(path: Path, *, catalog: tuple[str, ...]) -> tuple[str, ...]:
    try:
        selected = tuple(line.strip() for line in _read_private(path).decode().splitlines() if line.strip())
    except UnicodeDecodeError as exc:
        raise CampaignBlocked("Daytona campaign task file is not UTF-8") from exc
    if not selected or len(selected) != len(set(selected)):
        raise CampaignBlocked("Daytona campaign task file must be nonempty and unique")
    selected_set = set(selected)
    if tuple(task_id for task_id in catalog if task_id in selected_set) != selected:
        raise CampaignBlocked("Daytona campaign task file is unknown or not in deterministic catalog order")
    return selected


def _service_property(name: str) -> str:
    if name not in {"ActiveState", "MainPID", "UnitFileState"}:
        raise ValueError("unsupported systemd property")
    result = subprocess.run(  # noqa: S603 - property is restricted to the fixed allowlist above
        ["/usr/bin/systemctl", "show", "cybergym-campaign.service", "-p", name, "--value"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout.strip()


def _require_canonical_handoff(
    path: Path,
    *,
    catalog: tuple[str, ...],
    selected: tuple[str, ...],
) -> int:
    _require_canonical_quiescent()
    first = _read_private(path)
    time.sleep(0.05)
    second = _read_private(path)
    if first != second:
        raise CampaignBlocked("canonical CyberGym manifest changed during handoff")
    try:
        payload = json.loads(first)
    except json.JSONDecodeError as exc:
        raise CampaignBlocked("canonical CyberGym manifest is malformed") from exc
    if not isinstance(payload, dict) or payload.get("halt") is not None:
        raise CampaignBlocked("canonical CyberGym manifest is halted or malformed")
    identity = payload.get("identity")
    if not isinstance(identity, dict) or (
        identity.get("catalog_sha256") != _catalog_digest(catalog) or identity.get("task_count") != len(catalog)
    ):
        raise CampaignBlocked("canonical CyberGym manifest catalog identity drifted")
    shards = payload.get("shards")
    if not isinstance(shards, list):
        raise CampaignBlocked("canonical CyberGym manifest shards are malformed")
    completed: list[str] = []
    for shard in shards:
        if not isinstance(shard, dict) or not isinstance(shard.get("completed_task_ids"), list):
            raise CampaignBlocked("canonical CyberGym shard is malformed")
        completed.extend(shard["completed_task_ids"])
        attempts = shard.get("attempts")
        if not isinstance(attempts, list) or any(
            not isinstance(attempt, dict) or attempt.get("status") == "running" for attempt in attempts
        ):
            raise CampaignBlocked("canonical CyberGym manifest still contains running work")
    if len(completed) != len(set(completed)) or any(task_id not in set(catalog) for task_id in completed):
        raise CampaignBlocked("canonical CyberGym completion set is invalid")
    completed_set = set(completed)
    remaining = tuple(task_id for task_id in catalog if task_id not in completed_set)
    if selected != remaining:
        raise CampaignBlocked("Daytona selection is not the exact uncompleted canonical suffix")
    return len(completed)


def _require_canonical_quiescent() -> None:
    """Prove the independent lane cannot overlap the canonical paid runner."""

    if (
        _service_property("ActiveState") != "inactive"
        or _service_property("MainPID") != "0"
        or _service_property("UnitFileState") != "disabled"
    ):
        raise CampaignBlocked("canonical CyberGym campaign must be inactive, empty, and disabled")
    docker = subprocess.run(
        ["/usr/bin/docker", "ps", "--filter", "name=openhands-runtime-", "--format", "{{.Names}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if docker.stdout.strip():
        raise CampaignBlocked("canonical OpenHands containers remain during Daytona handoff")
    processes = subprocess.run(
        ["/usr/bin/pgrep", "-f", "openhands.core.main"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if processes.returncode not in {0, 1} or processes.stdout.strip():
        raise CampaignBlocked("canonical OpenHands controller processes remain during Daytona handoff")


def _require_daytona_preflight(path: Path) -> None:
    report = _read_private_json(path)
    required = {
        "schema_version": "1",
        "no_model_call": True,
        "image": DAYTONA_IMAGE,
        "network_policy": "daytona-funnel-host-cidr-allowlist-task-relay-v1",
        "workspace_stage_verified": True,
        "sandbox_id_recorded": True,
    }
    if any(report.get(key) != value for key, value in required.items()):
        raise CampaignBlocked("Daytona no-model preflight report drifted")


def _require_credentials() -> None:
    missing = [
        name for name in ("HUD_API_KEY", "OPENAI_API_KEY", "DAYTONA_API_KEY") if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise CampaignBlocked(f"Daytona campaign is missing required credential variables: {missing}")


def _require_openhands_bridge(repository_root: Path) -> None:
    poetry = os.environ.get("CG_OPENHANDS_POETRY", "").strip()
    if not poetry:
        candidate = Path.home() / ".local/bin/poetry"
        poetry = str(candidate) if candidate.is_file() else (shutil.which("poetry") or "")
    if not poetry:
        raise CampaignBlocked("Poetry is unavailable for the OpenHands bridge probe")
    openhands = repository_root / "examples/agents/openhands/openhands-repo"
    shim = repository_root / "integrations/hud/openhands_shim"
    allowed = {
        name: value
        for name, value in os.environ.items()
        if name
        in {
            "HOME",
            "PATH",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "POETRY_CACHE_DIR",
            "POETRY_VIRTUALENVS_PATH",
        }
    }
    allowed.update(
        {
            "PYTHONPATH": str(shim),
            "CYBERGYM_REASONING_EFFORT": "xhigh",
            "CYBERGYM_DAYTONA_ACTION_URL": "http://127.0.0.1:43210",
        }
    )
    code = (
        "from openhands.llm import async_llm,llm;"
        "assert getattr(llm.LLM.__init__,'_cybergym_gpt56_xhigh',False);"
        "assert getattr(async_llm.AsyncLLM._call_acompletion,'_cybergym_gpt56_xhigh',False)"
    )
    result = subprocess.run(  # noqa: S603 - executable is resolved from a fixed operator-controlled path
        [poetry, "run", "python", "-c", code],
        cwd=openhands,
        env=allowed,
        check=False,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise CampaignBlocked("pinned OpenHands GPT-5.6 bridge probe failed without a model call")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-paid-selection", action="store_true")
    parser.add_argument("--continue-after-errors", action="store_true")
    parser.add_argument("--independent-selection", action="store_true")
    parser.add_argument("--job-name", default=JOB_NAME)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--canonical-manifest", type=Path)
    parser.add_argument("--artifact-preflight-report", type=Path, required=True)
    parser.add_argument("--artifact-preflight-concurrency", type=int, default=6)
    parser.add_argument("--daytona-preflight-report", type=Path, required=True)
    parser.add_argument("--daytona-known-hosts", type=Path, required=True)
    parser.add_argument("--max-concurrent", type=int, default=60)
    parser.add_argument("--shard-size", type=int, default=60)
    parser.add_argument("--keep-tmp", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if not args.confirm_paid_selection:
        raise SystemExit("the Daytona paid selection requires --confirm-paid-selection")
    _require_credentials()
    root = args.repository_root.expanduser().resolve()
    catalog = catalog_task_ids(root)
    selected = _load_task_file(args.task_file.expanduser().resolve(), catalog=catalog)
    if args.independent_selection:
        _require_canonical_quiescent()
        canonical_completed = 0
    else:
        if args.canonical_manifest is None:
            raise SystemExit("the canonical handoff requires --canonical-manifest")
        canonical_completed = _require_canonical_handoff(
            args.canonical_manifest.expanduser().resolve(),
            catalog=catalog,
            selected=selected,
        )
    validate_daytona_contract()
    _require_daytona_preflight(args.daytona_preflight_report.expanduser().resolve())
    _require_openhands_bridge(root)
    results_dir = args.results_dir.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = NativeOpenHandsConfig(
        repository_root=root,
        data_dir=args.data_dir,
        server=args.server,
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
        log_dir=results_dir / "logs",
        tmp_dir=results_dir / "tmp",
        max_iter=200,
        timeout=3600,
        base_url=os.environ.get("CG_MODEL_BASE_URL", ""),
        grader_server_mode="binary",
        silent=True,
        remove_tmp=not args.keep_tmp,
        runtime_nano_cpus=CAMPAIGN_RUNTIME_NANO_CPUS,
        runtime_memory_bytes=CAMPAIGN_RUNTIME_MEMORY_BYTES,
        runtime_memory_swap_bytes=CAMPAIGN_RUNTIME_MEMORY_SWAP_BYTES,
        execution_backend="daytona-private",
        daytona_ledger_path=state_dir / "sandboxes.jsonl",
        daytona_known_hosts=args.daytona_known_hosts,
    ).normalized()
    config.log_dir.mkdir(parents=True, exist_ok=True)
    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        with campaign_lock(state_dir):
            reconcile_daytona_sandboxes(
                config.daytona_ledger_path,
                expected_task_ids=set(selected),
            )
            fingerprints = load_preflight_fingerprints(
                args.artifact_preflight_report.expanduser().resolve(),
                config=config,
                max_concurrent=args.artifact_preflight_concurrency,
            )
            summary = asyncio.run(
                run_campaign(
                    config,
                    state_dir=state_dir,
                    max_concurrent=args.max_concurrent,
                    shard_size=args.shard_size,
                    confirm_paid_all=True,
                    continue_after_errors=args.continue_after_errors,
                    artifact_fingerprints=fingerprints,
                    selected_task_ids=selected,
                    job_name=args.job_name,
                )
            )
    except CampaignBlocked as exc:
        raise SystemExit(f"Daytona campaign blocked: {exc}") from exc
    summary["canonical_completed_before_handoff"] = canonical_completed
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

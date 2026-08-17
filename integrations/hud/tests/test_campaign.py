from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from hud.agents.types import AgentStep

from cybergym_hud.campaign import (
    CAMPAIGN_JOB_NAME,
    CampaignBlocked,
    CampaignState,
    _campaign_identity,
    _catalog_digest,
    campaign_lock,
    load_preflight_fingerprints,
    reconcile_running_attempt,
    require_remote_job_receipt,
    require_remote_trace_enter,
    run_campaign,
    validate_campaign_profile,
)
from cybergym_hud.native import NativeOpenHandsConfig
from cybergym_hud.openhands_trace import ProjectedStep, build_trace_import_metadata
from cybergym_hud.receipt import NativeReceipt
from cybergym_hud.runtime_network import expected_network_attestation, network_attestation_sha256


def _remote_projected_events(*, grader_error: bool = False) -> list[dict[str, object]]:
    projected = ProjectedStep(
        "response:fixture",
        AgentStep(content="finished", done=True),
    )
    return [
        {"kind": "agent_message", "text": "finished", "reasoning": None, "tool_calls": []},
        {
            "kind": "raw",
            "attributes": {
                "openhands_trace_import": build_trace_import_metadata(
                    (projected,),
                    status="completed",
                )
            },
        },
        {
            "kind": "scenario_evaluate",
            "error": None if not grader_error else "grader failure",
            "result": {"done": True, "isError": grader_error, "score": 0.0},
        },
    ]


def _config(tmp_path: Path) -> NativeOpenHandsConfig:
    return NativeOpenHandsConfig(
        repository_root=Path(__file__).resolve().parents[3],
        data_dir=tmp_path / "data",
        server="http://172.30.0.1:8666",
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
        log_dir=tmp_path / "results/logs",
        tmp_dir=tmp_path / "results/tmp",
        max_iter=200,
        timeout=3600,
        silent=True,
        runtime_nano_cpus=4_000_000_000,
        runtime_memory_bytes=8 * 1024**3,
        runtime_memory_swap_bytes=8 * 1024**3,
        runtime_network="cybergym-no-internet",
    ).normalized()


def test_paid_campaign_profile_is_exact_and_width_is_capped(tmp_path: Path) -> None:
    config = _config(tmp_path)
    validate_campaign_profile(config, max_concurrent=6, shard_size=24)
    with pytest.raises(ValueError, match="max_concurrent"):
        validate_campaign_profile(config, max_concurrent=7, shard_size=12)
    with pytest.raises(ValueError, match="profile drift"):
        validate_campaign_profile(
            replace(config, max_iter=201),
            max_concurrent=1,
            shard_size=12,
        )
    daytona = replace(
        config,
        execution_backend="daytona-private",
        daytona_ledger_path=tmp_path / "daytona.jsonl",
        daytona_known_hosts=tmp_path / "known-hosts",
    )
    validate_campaign_profile(daytona, max_concurrent=60, shard_size=60)
    with pytest.raises(ValueError, match="max_concurrent"):
        validate_campaign_profile(daytona, max_concurrent=61, shard_size=60)


def test_paid_campaign_requires_matching_no_internet_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ids = ("arvo:1", "oss-fuzz:2")
    monkeypatch.setattr("cybergym_hud.campaign.catalog_task_ids", lambda _root: ids)
    network = expected_network_attestation(server_url=config.server)
    report = {
        "schema_version": "1",
        "no_model_call": True,
        "catalog_sha256": _catalog_digest(ids),
        "task_count": len(ids),
        "grader_server_mode": "images",
        "max_concurrent": 6,
        "runtime_limits": {
            "nano_cpus": 4_000_000_000,
            "memory": 8 * 1024**3,
            "memory_swap": 8 * 1024**3,
        },
        "runtime_network": network,
        "runtime_network_sha256": network_attestation_sha256(network),
        "source_artifact_sha256": "1" * 64,
        "grader_artifact_sha256": "2" * 64,
        "source_provenance_sha256": "3" * 64,
        "source_selected_manifest_sha256": "4" * 64,
    }
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    path.chmod(0o600)

    fingerprints = load_preflight_fingerprints(path, config=config, max_concurrent=6)
    assert fingerprints["runtime_network_sha256"] == network_attestation_sha256(network)

    report["runtime_network"] = {**network, "public_dns_blocked": False}
    path.write_text(json.dumps(report), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(CampaignBlocked, match="does not match"):
        load_preflight_fingerprints(path, config=config, max_concurrent=6)


def test_state_is_deterministic_mode_0600_and_contains_no_credentials(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ids = ("arvo:1", "arvo:2", "oss-fuzz:3")
    state = CampaignState(tmp_path / "state")
    state.state_dir.mkdir(mode=0o700)
    state.initialize(identity=_campaign_identity(config, ids, 2), task_ids=ids, shard_size=2)

    saved = json.loads(state.path.read_text(encoding="utf-8"))
    assert [shard["task_ids"] for shard in saved["shards"]] == [["arvo:1", "arvo:2"], ["oss-fuzz:3"]]
    assert state.path.stat().st_mode & 0o777 == 0o600
    serialized = json.dumps(saved)
    assert "API_KEY" not in serialized
    assert "sk-" not in serialized
    assert config.base_url not in serialized or not config.base_url

    with pytest.raises(CampaignBlocked, match="identity/profile"):
        other = CampaignState(tmp_path / "state")
        other.initialize(identity=_campaign_identity(config, ids, 1), task_ids=ids, shard_size=1)

    separate = _campaign_identity(config, ids, 2, job_name="cybergym-gpt5.6-sol-2")
    assert separate["job_name"] == "cybergym-gpt5.6-sol-2"


@pytest.mark.asyncio
async def test_campaign_rejects_unknown_or_reordered_selected_task_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr("cybergym_hud.campaign.catalog_task_ids", lambda _root: ("arvo:1", "arvo:2"))
    for selected in (("arvo:unknown",), ("arvo:2", "arvo:1")):
        with pytest.raises(ValueError, match="selection"):
            await run_campaign(
                config,
                state_dir=tmp_path / "state",
                max_concurrent=1,
                shard_size=1,
                confirm_paid_all=True,
                artifact_fingerprints={},
                selected_task_ids=selected,
            )


def test_lock_refuses_a_second_campaign_process(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    with campaign_lock(state_dir):
        with pytest.raises(CampaignBlocked, match="another campaign"):
            with campaign_lock(state_dir):
                pass


@pytest.mark.asyncio
async def test_campaign_checkpoints_small_named_jobs_and_restart_skips_paid_tasks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ids = ("arvo:1", "arvo:2", "arvo:3", "arvo:4", "oss-fuzz:5")
    monkeypatch.setattr("cybergym_hud.campaign.catalog_task_ids", lambda _root: ids)
    monkeypatch.setattr("cybergym_hud.campaign.validate_contract", lambda **_kwargs: None)
    monkeypatch.setattr(
        "cybergym_hud.campaign.make_taskset",
        lambda **_kwargs: SimpleNamespace(api_id=None),
    )
    jobs: list[SimpleNamespace] = []
    launched: list[str] = []

    async def job_factory(name: str, *, taskset_id=None):
        assert name == CAMPAIGN_JOB_NAME
        assert taskset_id is None
        job = SimpleNamespace(id=f"job-{len(jobs) + 1}", name=name)
        jobs.append(job)
        return job

    async def job_receipt_verifier(job, *, client=None):
        assert client is None
        assert job in jobs

    def native_executor(rollout_config, binding):
        launched.append(binding.task_id)
        return NativeReceipt(
            status="completed",
            task_id=binding.task_id,
            server=binding.server,
            run_profile=rollout_config.receipt_profile(),
            agent_id="a" * 32,
            upstream_returned_agent_id="a" * 32,
        )

    async def batch_runner(
        task_ids,
        rollout_config,
        *,
        executor,
        job_name,
        job,
        prelaunch_verifier,
        **_kwargs,
    ):
        assert job_name == CAMPAIGN_JOB_NAME == job.name
        assert callable(prelaunch_verifier)
        runs = []
        for task_id in task_ids:
            receipt = executor(
                rollout_config,
                SimpleNamespace(task_id=task_id, server=rollout_config.server),
            )
            runs.append(
                {
                    "trace_id": f"trace-{task_id}",
                    "is_error": False,
                    "reward": 1.0,
                    "native_receipt": receipt.model_dump(mode="json"),
                    "openhands_trace_import": {
                        "schema_version": "1",
                        "status": "completed",
                        "projected_step_count": 1,
                        "agent_step_count": 1,
                        "tool_step_count": 0,
                        "user_step_count": 0,
                        "source_has_tool_actions": False,
                        "projected_steps_sha256": "a" * 64,
                    },
                }
            )
        return {
            "job_id": job.id,
            "job_name": job.name,
            "task_count": len(runs),
            "is_error": False,
            "runs": runs,
        }

    async def receipt_verifier(result, *, results_dir):
        assert results_dir == config.log_dir.parent
        return {
            **result,
            "trace_ids": [run["trace_id"] for run in result["runs"]],
            "hud_remote_receipt_verified": True,
            "hud_remote_events_verified": True,
        }

    state_dir = tmp_path / "state"
    summary = await run_campaign(
        config,
        state_dir=state_dir,
        max_concurrent=2,
        shard_size=2,
        confirm_paid_all=True,
        job_factory=job_factory,
        job_receipt_verifier=job_receipt_verifier,
        batch_runner=batch_runner,
        receipt_verifier=receipt_verifier,
        native_executor=native_executor,
        artifact_fingerprints={"source_artifact_sha256": "1" * 64, "grader_artifact_sha256": "2" * 64},
    )
    assert summary["complete"] is True
    assert launched == list(ids)
    assert len(jobs) == 3
    assert all(job.name == CAMPAIGN_JOB_NAME for job in jobs)
    assert len(list((state_dir / "shards").glob("*.json"))) == 3

    async def forbidden_job_factory(*_args, **_kwargs):
        raise AssertionError("restart must not open a new paid job")

    resumed = await run_campaign(
        config,
        state_dir=state_dir,
        max_concurrent=1,
        shard_size=2,
        confirm_paid_all=True,
        job_factory=forbidden_job_factory,
        job_receipt_verifier=job_receipt_verifier,
        batch_runner=batch_runner,
        receipt_verifier=receipt_verifier,
        native_executor=native_executor,
        artifact_fingerprints={"source_artifact_sha256": "1" * 64, "grader_artifact_sha256": "2" * 64},
    )
    assert resumed["complete"] is True
    assert launched == list(ids)


@pytest.mark.asyncio
async def test_paid_job_must_be_remotely_acknowledged_before_launch() -> None:
    job = SimpleNamespace(id="a" * 32, name=CAMPAIGN_JOB_NAME)

    class Client:
        async def aget(self, path, *, params=None):
            if path.endswith("/traces"):
                assert params == {"limit": 1, "offset": 0}
                return {"items": []}
            assert path == f"/jobs/{job.id}" and params is None
            return {
                "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "name": CAMPAIGN_JOB_NAME,
                "can_edit": True,
                "group_size": 1,
                "taskset_id": None,
            }

    await require_remote_job_receipt(job, client=Client())

    class WrongNameClient:
        async def aget(self, _path, *, params=None):
            assert params is None
            return {
                "id": job.id,
                "name": "wrong",
                "can_edit": True,
                "group_size": 1,
                "taskset_id": None,
            }

    with pytest.raises(CampaignBlocked, match="did not acknowledge"):
        await require_remote_job_receipt(job, client=WrongNameClient())


@pytest.mark.asyncio
async def test_paid_trace_must_be_remotely_acknowledged_with_exact_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace_id = "b" * 32

    class Client:
        async def aget(self, path, *, params=None):
            assert path == "/jobs/job-1/traces"
            assert params == {"limit": 1000, "offset": 0}
            return {
                "items": [
                    {
                        "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                        "task_slug": "arvo-1",
                    }
                ]
            }

    await require_remote_trace_enter("job-1", trace_id, "arvo:1", client=Client())

    class WrongSlugClient(Client):
        async def aget(self, path, *, params=None):
            rows = await super().aget(path, params=params)
            rows["items"][0]["task_slug"] = "arvo-2"
            return rows

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("cybergym_hud.campaign.asyncio.sleep", no_sleep)
    with pytest.raises(CampaignBlocked, match="mismatched task slug"):
        await require_remote_trace_enter("job-1", trace_id, "arvo:1", client=WrongSlugClient())


@pytest.mark.asyncio
async def test_restart_recovers_terminal_rows_and_leaves_unlaunched_rows_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ids = ("arvo:1", "arvo:2")
    state = CampaignState(tmp_path / "state")
    state.state_dir.mkdir(mode=0o700)
    state.initialize(identity=_campaign_identity(config, ids, 2), task_ids=ids, shard_size=2)
    attempt_number = state.start_attempt(
        0,
        job=SimpleNamespace(id="job-1", name=CAMPAIGN_JOB_NAME),
        task_ids=ids,
        max_concurrent=1,
    )
    state.mark_launched(0, attempt_number, "arvo:1")

    class Client:
        async def aget(self, path, *, params=None):
            if path == "/trace/trace-1/events":
                assert params is None
                return {"events": _remote_projected_events()}
            assert params["offset"] == 0
            return [
                {
                    "id": "trace-1",
                    "task_slug": "arvo-1",
                    "status": "completed",
                    "reward": 0.0,
                }
            ]

    await reconcile_running_attempt(state, 0, state.shard(0)["attempts"][0], client=Client())
    assert state.shard(0)["completed_task_ids"] == ["arvo:1"]
    assert state.pending_task_ids(0) == ("arvo:2",)


@pytest.mark.asyncio
async def test_restart_does_not_mark_prelaunch_remote_trace_complete(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ids = ("arvo:1",)
    state = CampaignState(tmp_path / "state")
    state.state_dir.mkdir(mode=0o700)
    state.initialize(identity=_campaign_identity(config, ids, 1), task_ids=ids, shard_size=1)
    state.start_attempt(
        0,
        job=SimpleNamespace(id="job-1", name=CAMPAIGN_JOB_NAME),
        task_ids=ids,
        max_concurrent=1,
    )

    class Client:
        async def aget(self, _path, *, params=None):
            assert params == {"limit": 1000, "offset": 0}
            return [{"id": "trace-1", "task_slug": "arvo-1", "status": "error", "reward": 0.0}]

    await reconcile_running_attempt(state, 0, state.shard(0)["attempts"][0], client=Client())
    assert state.shard(0)["completed_task_ids"] == []
    assert state.pending_task_ids(0) == ids


@pytest.mark.asyncio
async def test_restart_halts_on_completed_trace_with_remote_grader_error(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ids = ("arvo:1",)
    state = CampaignState(tmp_path / "state")
    state.state_dir.mkdir(mode=0o700)
    state.initialize(identity=_campaign_identity(config, ids, 1), task_ids=ids, shard_size=1)
    attempt_number = state.start_attempt(
        0,
        job=SimpleNamespace(id="job-1", name=CAMPAIGN_JOB_NAME),
        task_ids=ids,
        max_concurrent=1,
    )
    state.mark_launched(0, attempt_number, "arvo:1")

    class Client:
        async def aget(self, path, *, params=None):
            if path == "/trace/trace-1/events":
                return {"events": _remote_projected_events(grader_error=True)}
            return [
                {
                    "id": "trace-1",
                    "task_slug": "arvo-1",
                    "status": "completed",
                    "reward": 0.0,
                }
            ]

    await reconcile_running_attempt(state, 0, state.shard(0)["attempts"][0], client=Client())
    assert state.payload["halt"]["job_id"] == "job-1"
    assert state.shard(0)["status"] == "verified_with_errors"


@pytest.mark.asyncio
async def test_restart_blocks_instead_of_repeating_a_launched_task_without_receipt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ids = ("arvo:1",)
    state = CampaignState(tmp_path / "state")
    state.state_dir.mkdir(mode=0o700)
    state.initialize(identity=_campaign_identity(config, ids, 1), task_ids=ids, shard_size=1)
    attempt_number = state.start_attempt(
        0,
        job=SimpleNamespace(id="job-1", name=CAMPAIGN_JOB_NAME),
        task_ids=ids,
        max_concurrent=1,
    )
    state.mark_launched(0, attempt_number, "arvo:1")

    class EmptyClient:
        async def aget(self, _path, *, params):
            return []

    with pytest.raises(CampaignBlocked, match="No task was relaunched"):
        await reconcile_running_attempt(state, 0, state.shard(0)["attempts"][0], client=EmptyClient())
    assert state.shard(0)["attempts"][0]["status"] == "reconciliation_required"

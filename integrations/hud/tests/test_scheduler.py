from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from hud import Taskset
from hud.eval.runtime import LocalRuntime
from hud.graders import EvaluationResult

from cybergym_hud.env import build_env
from cybergym_hud.native import NativeOpenHandsAgent, NativeOpenHandsConfig
from cybergym_hud.receipt import NativeReceipt, NativeTaskBinding
from cybergym_hud.scheduler import (
    _parser,
    _resolve_selection,
    prepare_tracked_rollout,
    require_remote_hud_receipt,
    run_many,
    run_one,
    summarize_job,
    verify_and_persist_remote_receipt,
)
from cybergym_hud.tasks import make_task
from cybergym_hud.taskset import task_ids


@pytest.mark.asyncio
async def test_end_to_end_hud_receipt_and_upstream_grade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *args):
            return

        def do_POST(self):  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            calls.append((self.path, body))
            if self.path == "/verify-agent-pocs":
                payload = {"poc_ids": ["poc-1"]}
            elif self.path == "/query-poc":
                payload = [
                    {
                        "agent_id": "a" * 32,
                        "task_id": "arvo:10013",
                        "poc_id": "poc-1",
                        "vul_exit_code": 1,
                        "fix_exit_code": 0,
                    }
                ]
            else:
                self.send_response(404)
                self.end_headers()
                return
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("CYBERGYM_API_KEY", "secret")

    config = NativeOpenHandsConfig(
        repository_root=Path(__file__).resolve().parents[3],
        data_dir=tmp_path / "data",
        server=base_url,
        model="fake",
        log_dir=tmp_path / "logs",
        tmp_dir=tmp_path / "tmp",
    )

    def executor(_config, binding):
        return NativeReceipt(
            status="completed",
            task_id=binding.task_id,
            server=binding.server,
            run_profile=config.receipt_profile(),
            agent_id="a" * 32,
            upstream_returned_agent_id="a" * 32,
            log_dir=str(tmp_path / "logs/run"),
        )

    try:
        taskset = Taskset("receipt", [make_task("arvo:10013", server=base_url)])
        job = await taskset.run(
            NativeOpenHandsAgent(config, executor=executor),
            runtime=LocalRuntime(lambda _task: build_env(file_tracking_root=config.tmp_dir)),
            max_concurrent=1,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    summary = summarize_job(job)
    assert summary["reward"] == 1.0
    assert summary["is_error"] is False
    assert summary["native_receipt"]["agent_id"] == "a" * 32
    assert [path for path, _body in calls] == ["/verify-agent-pocs", "/query-poc"]


def test_tracked_rollouts_are_unique_and_defer_upstream_cleanup(tmp_path: Path) -> None:
    config = NativeOpenHandsConfig(
        repository_root=Path(__file__).resolve().parents[3],
        data_dir=tmp_path / "data",
        server="http://127.0.0.1:8666",
        model="fake",
        log_dir=tmp_path / "logs",
        tmp_dir=tmp_path / "tmp",
        remove_tmp=True,
    )

    first, first_cleanup = prepare_tracked_rollout(config, uuid_factory=lambda: UUID(int=1))
    second, second_cleanup = prepare_tracked_rollout(config, uuid_factory=lambda: UUID(int=2))

    assert first.tmp_dir != second.tmp_dir
    assert first.tmp_dir.parent == second.tmp_dir.parent == config.tmp_dir
    assert first.tmp_dir.is_dir() and second.tmp_dir.is_dir()
    assert first.remove_tmp is second.remove_tmp is False
    assert first_cleanup is second_cleanup is True


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_tmp", [True, False])
async def test_run_one_flushes_final_workspace_before_applying_cleanup_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    remove_tmp: bool,
) -> None:
    from hud.eval import file_tracking as observer
    from hud.settings import settings

    config = NativeOpenHandsConfig(
        repository_root=Path(__file__).resolve().parents[3],
        data_dir=tmp_path / "data",
        server="http://127.0.0.1:8666",
        model="fake",
        log_dir=tmp_path / "logs",
        tmp_dir=tmp_path / "tmp",
        remove_tmp=remove_tmp,
    )
    rollout_uuid = UUID(int=7)
    observed_root = config.tmp_dir / f"hud-rollout-{rollout_uuid.hex}"
    emitted: list[tuple[str, dict[str, object]]] = []

    class WorkspaceWritingAgent:
        def __init__(
            self,
            rollout_config: NativeOpenHandsConfig,
            *,
            worker_pool=None,
        ) -> None:
            self.config = rollout_config
            assert worker_pool is not None

        async def __call__(self, run) -> None:
            assert self.config.tmp_dir == observed_root
            assert self.config.remove_tmp is False
            binding = NativeTaskBinding.model_validate_json(run.prompt_text)
            workspace = observed_root / f"arvo_10013-{'a' * 32}" / "workspace"
            workspace.mkdir(parents=True)
            (workspace / "poc").write_bytes(b"final exploit input")
            receipt = NativeReceipt(
                status="completed",
                task_id=binding.task_id,
                server=binding.server,
                run_profile=self.config.receipt_profile(),
                agent_id="a" * 32,
                upstream_returned_agent_id="a" * 32,
                log_dir=str(tmp_path / "logs/run"),
            )
            run.trace.content = receipt.model_dump_json()
            run.trace.stop_reason = "done"

    async def _grade(_binding, _receipt):
        return EvaluationResult(reward=0.0, content="test grade")

    def _emit(span_name, payload, *, started_at, ended_at=None):
        del started_at, ended_at
        emitted.append((span_name, payload))
        return True

    monkeypatch.setattr(
        "cybergym_hud.scheduler.prepare_tracked_rollout",
        lambda incoming: prepare_tracked_rollout(incoming, uuid_factory=lambda: rollout_uuid),
    )
    monkeypatch.setattr("cybergym_hud.scheduler.validate_contract", lambda **_kwargs: None)
    monkeypatch.setattr("cybergym_hud.scheduler.NativeOpenHandsAgent", WorkspaceWritingAgent)
    monkeypatch.setattr("cybergym_hud.env.grade_receipt", _grade)
    monkeypatch.setattr(observer, "_emit_file_tracking", _emit)
    monkeypatch.setattr(settings, "telemetry_enabled", True)
    monkeypatch.setattr(settings, "file_tracking_interval", 60.0)

    await run_one("arvo:10013", config)

    changed_paths = {
        patch["path"]
        for span_name, payload in emitted
        if span_name == "filetracking.diff"
        for patch in payload.get("patches", [])
    }
    assert f"arvo_10013-{'a' * 32}/workspace/poc" in changed_paths
    assert observed_root.exists() is (not remove_tmp)


@pytest.mark.asyncio
async def test_run_many_uses_a_rolling_fifteen_slot_window_with_isolated_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from hud.eval import file_tracking as observer
    from hud.settings import settings

    selected = task_ids(Path(__file__).resolve().parents[3])[:17]
    config = NativeOpenHandsConfig(
        repository_root=Path(__file__).resolve().parents[3],
        data_dir=tmp_path / "data",
        server="http://127.0.0.1:8666",
        model="fake",
        log_dir=tmp_path / "logs",
        tmp_dir=tmp_path / "tmp",
        max_iter=73,
        timeout=911,
        top_p=0.75,
        temperature=0.2,
        max_output_tokens=4096,
        seed=44,
        silent=True,
        remove_tmp=True,
        debug=True,
    )
    roots: dict[str, Path] = {}
    started: list[str] = []
    active = 0
    max_active = 0
    lock = threading.Lock()
    first_wave_started = threading.Event()
    sixteenth_started = threading.Event()
    seventeenth_started = threading.Event()
    release_first = threading.Event()
    release_sixteenth = threading.Event()
    release_rest = threading.Event()
    emitted_paths: set[str] = set()
    expected_config = config.normalized()

    unchanged_fields = {
        field.name for field in fields(NativeOpenHandsConfig) if field.name not in {"tmp_dir", "remove_tmp"}
    }

    def executor(rollout_config: NativeOpenHandsConfig, binding: NativeTaskBinding) -> NativeReceipt:
        nonlocal active, max_active
        assert all(getattr(rollout_config, name) == getattr(expected_config, name) for name in unchanged_fields)
        assert rollout_config.remove_tmp is False
        workspace = rollout_config.tmp_dir / f"{binding.task_id.replace(':', '_')}-{'a' * 32}" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "poc").write_text(binding.task_id, encoding="utf-8")

        with lock:
            roots[binding.task_id] = rollout_config.tmp_dir
            started.append(binding.task_id)
            active += 1
            max_active = max(max_active, active)
            if all(task_id in started for task_id in selected[:15]):
                first_wave_started.set()
            if binding.task_id == selected[15]:
                sixteenth_started.set()
            if binding.task_id == selected[16]:
                seventeenth_started.set()

        if binding.task_id == selected[0]:
            assert release_first.wait(timeout=10)
        elif binding.task_id in selected[:15]:
            assert release_rest.wait(timeout=10)
        elif binding.task_id == selected[15]:
            assert release_sixteenth.wait(timeout=10)

        with lock:
            active -= 1
        return NativeReceipt(
            status="completed",
            task_id=binding.task_id,
            server=binding.server,
            run_profile=rollout_config.receipt_profile(),
            agent_id="a" * 32,
            upstream_returned_agent_id="a" * 32,
            log_dir=str(rollout_config.log_dir / binding.task_id.replace(":", "_")),
        )

    async def _grade(_binding, _receipt):
        return EvaluationResult(reward=0.0, content="test grade")

    def _emit(span_name, payload, *, started_at, ended_at=None):
        del started_at, ended_at
        if span_name == "filetracking.diff":
            emitted_paths.update(patch["path"] for patch in payload.get("patches", []))
        return True

    uuid_values = iter(UUID(int=index) for index in range(1, 18))
    monkeypatch.setattr("cybergym_hud.scheduler.validate_contract", lambda **_kwargs: None)
    monkeypatch.setattr("cybergym_hud.env.grade_receipt", _grade)
    monkeypatch.setattr(observer, "_emit_file_tracking", _emit)
    monkeypatch.setattr(settings, "telemetry_enabled", True)
    monkeypatch.setattr(settings, "file_tracking_interval", 60.0)

    batch = asyncio.create_task(
        run_many(
            selected,
            config,
            max_concurrent=15,
            executor=executor,
            uuid_factory=lambda: next(uuid_values),
        )
    )
    assert await asyncio.to_thread(first_wave_started.wait, 10)
    assert max_active == 15
    assert selected[15] not in started

    release_first.set()
    assert await asyncio.to_thread(sixteenth_started.wait, 10)
    # The sixteenth slot is rolling: it begins while fourteen first-wave
    # rollouts are still blocked, after the completed slot flushed and cleaned.
    assert not release_rest.is_set()
    assert all(roots[task_id].exists() for task_id in selected[1:15])
    assert roots[selected[0]].exists() is False
    assert max_active <= 15
    assert not seventeenth_started.is_set()

    release_sixteenth.set()
    assert await asyncio.to_thread(seventeenth_started.wait, 10)
    assert not release_rest.is_set()
    assert all(roots[task_id].exists() for task_id in selected[1:15])

    release_rest.set()
    result = await asyncio.wait_for(batch, timeout=20)

    assert result["task_count"] == 17
    assert [run["native_receipt"]["task_id"] for run in result["runs"]] == list(selected)
    assert len(set(roots.values())) == 17
    assert all(not root.exists() for root in roots.values())
    assert any(path.endswith("/workspace/poc") for path in emitted_paths)


def test_batch_selection_requires_an_explicit_full_catalog_paid_guard() -> None:
    root = Path(__file__).resolve().parents[3]
    parser = _parser()
    first_two = SimpleNamespace(
        repository_root=root,
        task_ids=[],
        all=False,
        first_n=2,
        confirm_paid_all=False,
        max_concurrent=15,
    )
    assert _resolve_selection(first_two, parser) == task_ids(root)[:2]

    paid_all = SimpleNamespace(
        repository_root=root,
        task_ids=[],
        all=True,
        first_n=None,
        confirm_paid_all=False,
        max_concurrent=15,
    )
    with pytest.raises(SystemExit):
        _resolve_selection(paid_all, parser)
    paid_all.confirm_paid_all = True
    assert _resolve_selection(paid_all, parser) == task_ids(root)

    paid_all.max_concurrent = 16
    with pytest.raises(SystemExit):
        _resolve_selection(paid_all, parser)


@pytest.mark.asyncio
async def test_remote_hud_receipt_is_false_first_then_verified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed_false_first = False

    async def _verify(job_id: str, trace_ids: tuple[str, ...]) -> tuple[str, ...]:
        nonlocal observed_false_first
        saved = json.loads((tmp_path / "hud-summary-job-1.json").read_text(encoding="utf-8"))
        observed_false_first = saved["hud_remote_receipt_verified"] is False
        assert job_id == "job-1"
        assert trace_ids == ("a" * 32,)
        return ("11111111-1111-1111-1111-111111111111",)

    monkeypatch.setattr("cybergym_hud.scheduler.require_remote_hud_receipt", _verify)
    result = {
        "job_id": "job-1",
        "trace_id": "a" * 32,
        "status": "completed",
        "reward": 1.0,
        "is_error": False,
    }
    summary = await verify_and_persist_remote_receipt(result, results_dir=tmp_path)

    assert observed_false_first is True
    assert summary["hud_remote_receipt_verified"] is True
    assert summary["hud_remote_events_verified"] is True
    assert summary["trace_ids"] == ["11111111-1111-1111-1111-111111111111"]
    saved = json.loads((tmp_path / "hud-summary-job-1.json").read_text(encoding="utf-8"))
    assert saved == summary
    assert (tmp_path / "hud-summary-job-1.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_remote_hud_failure_retains_unverified_local_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def _fail(_job_id: str, _trace_ids: tuple[str, ...]) -> tuple[str, ...]:
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr("cybergym_hud.scheduler.require_remote_hud_receipt", _fail)
    result = {
        "job_id": "job-1",
        "trace_id": "a" * 32,
        "status": "completed",
        "reward": 1.0,
        "is_error": False,
    }
    with pytest.raises(RuntimeError, match="telemetry unavailable"):
        await verify_and_persist_remote_receipt(result, results_dir=tmp_path)

    saved = json.loads((tmp_path / "hud-summary-job-1.json").read_text(encoding="utf-8"))
    assert saved["hud_remote_receipt_verified"] is False
    assert saved["hud_remote_events_verified"] is False


@pytest.mark.asyncio
async def test_remote_hud_receipt_paginates_full_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[tuple[str, dict[str, int] | None]] = []
    trace_ids = tuple(UUID(int=index + 1).hex for index in range(1507))

    class Client:
        async def aget(self, path: str, *, params: dict[str, int] | None = None):
            requested.append((path, params))
            if path.endswith("/events"):
                return {"events": [{"type": "step"}]}
            assert params is not None
            start = params["offset"]
            stop = min(start + params["limit"], len(trace_ids))
            return {
                "items": [
                    {"id": str(UUID(value)), "status": "completed", "reward": 0.0} for value in trace_ids[start:stop]
                ]
            }

    monkeypatch.setattr(
        "cybergym_hud.scheduler.PlatformClient.from_settings",
        lambda: Client(),
    )
    remote = await require_remote_hud_receipt("job-full", trace_ids)
    receipt_requests = [params for path, params in requested if path.endswith("/traces")]
    assert receipt_requests == [{"limit": 1000, "offset": 0}, {"limit": 507, "offset": 1000}]
    assert len(remote) == 1507

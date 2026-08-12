from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

import pytest
from hud import Taskset
from hud.eval.runtime import LocalRuntime
from hud.graders import EvaluationResult

from cybergym_hud.env import build_env
from cybergym_hud.native import NativeOpenHandsAgent, NativeOpenHandsConfig
from cybergym_hud.receipt import NativeReceipt, NativeTaskBinding
from cybergym_hud.scheduler import prepare_tracked_rollout, run_one, summarize_job
from cybergym_hud.tasks import make_task


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
        def __init__(self, rollout_config: NativeOpenHandsConfig) -> None:
            self.config = rollout_config

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

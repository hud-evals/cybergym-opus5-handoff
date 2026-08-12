from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from hud import Taskset
from hud.eval.runtime import LocalRuntime

from cybergym_hud.env import build_env
from cybergym_hud.native import NativeOpenHandsAgent, NativeOpenHandsConfig
from cybergym_hud.receipt import NativeReceipt
from cybergym_hud.scheduler import summarize_job
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
            runtime=LocalRuntime(lambda _task: build_env()),
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

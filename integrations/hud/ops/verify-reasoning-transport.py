#!/usr/bin/env python3
"""Prove the pinned OpenHands request shape against localhost without spend."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL = "gpt-5.6-sol"
EFFORT = "xhigh"


class CaptureHandler(BaseHTTPRequestHandler):
    request_json: dict[str, Any] | None = None

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        type(self).request_json = json.loads(self.rfile.read(length))
        body = json.dumps(
            {
                "id": "chatcmpl-local-proof",
                "object": "chat.completion",
                "created": 0,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "local proof"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


CHILD = r"""
from openhands.core.config import LLMConfig
from openhands.llm.llm import LLM

config = LLMConfig(
    model="openai/gpt-5.6-sol",
    api_key="local-fake-key",
    base_url=__import__("os").environ["CYBERGYM_LOCAL_PROOF_BASE_URL"],
    max_output_tokens=32,
    num_retries=0,
)
llm = LLM(config)
assert llm.is_function_calling_active()
response = llm.completion(messages=[{"role": "user", "content": "local proof"}])
assert response.choices[0].message.content == "local proof"
"""


def prove(repository_root: Path) -> dict[str, Any]:
    repository_root = repository_root.resolve()
    openhands_root = repository_root / "examples/agents/openhands/openhands-repo"
    shim_dir = repository_root / "integrations/hud/openhands_shim"
    poetry = shutil.which("poetry")
    if not poetry:
        raise RuntimeError("poetry is unavailable")
    if not (openhands_root / "pyproject.toml").is_file() or not (shim_dir / "sitecustomize.py").is_file():
        raise RuntimeError("pinned OpenHands checkout or compatibility shim is missing")

    resolver_env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ.get("PATH", ""),
    }
    resolved = subprocess.run(  # noqa: S603
        [poetry, "env", "info", "--executable"],
        cwd=openhands_root,
        env=resolver_env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    project_python = Path(resolved.stdout.strip())
    if not project_python.is_file():
        raise RuntimeError(f"pinned OpenHands Poetry interpreter is missing: {project_python}")

    CaptureHandler.request_json = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            "HOME": os.environ.get("HOME", ""),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(shim_dir),
            "CYBERGYM_REASONING_EFFORT": EFFORT,
            "CYBERGYM_LOCAL_PROOF_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
        }
        try:
            subprocess.run(  # noqa: S603
                [str(project_python), "-c", CHILD],
                cwd=openhands_root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except subprocess.CalledProcessError as exc:
            diagnostic = (exc.stderr or exc.stdout or "no child diagnostic")[-4000:]
            raise RuntimeError(f"pinned OpenHands local request proof failed: {diagnostic}") from exc
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    request = CaptureHandler.request_json
    if not isinstance(request, dict):
        raise RuntimeError("pinned OpenHands made no captured Chat Completions request")
    expected = {"model": MODEL, "reasoning_effort": EFFORT}
    for key, value in expected.items():
        if request.get(key) != value:
            raise RuntimeError(f"captured {key} was {request.get(key)!r}, expected {value!r}")
    forbidden = sorted({"temperature", "top_p", "stop"} & request.keys())
    if forbidden:
        raise RuntimeError(f"captured request contains unsupported sampling controls: {forbidden}")
    return {
        "ok": True,
        "endpoint": "localhost-only-chat-completions",
        "model": request["model"],
        "reasoning_effort": request["reasoning_effort"],
        "forbidden_parameters_absent": ["temperature", "top_p", "stop"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prove(args.repository_root), sort_keys=True))


if __name__ == "__main__":
    main()

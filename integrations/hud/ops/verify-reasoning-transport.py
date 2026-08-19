#!/usr/bin/env python3
"""Prove pinned OpenHands' two-turn Responses bridge against localhost."""

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
RUNTIME_NETWORK = "cybergym-no-internet"


def _response(response_id: str, output: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {
            "id": response_id,
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "max_output_tokens": 32,
            "model": MODEL,
            "output": output,
            "parallel_tool_calls": True,
            "previous_response_id": None,
            "reasoning": {"effort": EFFORT, "summary": None},
            "store": True,
            "temperature": None,
            "tool_choice": "auto",
            "tools": [],
            "top_p": None,
            "truncation": "disabled",
            "usage": {
                "input_tokens": 2,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 2,
                "output_tokens_details": {"reasoning_tokens": 1},
                "total_tokens": 4,
            },
            "user": None,
        }
    ).encode()


class CaptureHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        type(self).requests.append(request)
        if len(type(self).requests) == 1:
            body = _response(
                "resp_local_1",
                [
                    {"id": "rs_local_1", "type": "reasoning", "summary": [], "status": "completed"},
                    {
                        "id": "fc_local_1",
                        "type": "function_call",
                        "call_id": "call_local_1",
                        "name": "execute_bash",
                        "arguments": '{"command":"pwd"}',
                        "status": "completed",
                    },
                ],
            )
        elif len(type(self).requests) == 2:
            body = _response(
                "resp_local_2",
                [
                    {
                        "id": "msg_local_2",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "local proof complete",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            )
        else:
            self.send_error(409)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


CHILD = r"""
from openhands.agenthub.codeact_agent.function_calling import get_tools
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
# The default OpenHands condenser constructs another LLM from this same
# config.  Prove startup attached separate state instead of a process singleton.
condenser_llm = LLM(config)
assert (
    llm._cybergym_gpt56_responses_bridge
    is not condenser_llm._cybergym_gpt56_responses_bridge
)
tools = get_tools(llm=llm)
messages = [
    {"role": "system", "content": "CodeAct local transport proof"},
    {"role": "user", "content": "Inspect the workspace"},
]
first = llm.completion(messages=messages, tools=tools)
call = first.choices[0].message.tool_calls[0]
assert call.id == "call_local_1"
assert call.function.name == "execute_bash"
messages.append(first.choices[0].message.model_dump())
messages.append(
    {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.function.name,
        "content": "/workspace\n",
    }
)
second = llm.completion(messages=messages, tools=tools)
assert second.choices[0].message.content == "local proof complete"
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

    CaptureHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            "HOME": os.environ.get("HOME", ""),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(shim_dir),
            "CYBERGYM_REASONING_EFFORT": EFFORT,
            "CYBERGYM_RUNTIME_NETWORK": RUNTIME_NETWORK,
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
            raise RuntimeError(f"pinned OpenHands local Responses proof failed: {diagnostic}") from exc
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    requests = CaptureHandler.requests
    if len(requests) != 2:
        raise RuntimeError(f"pinned OpenHands made {len(requests)} Responses requests, expected two")
    first, second = requests
    if first.get("model") != MODEL or first.get("reasoning") != {"effort": EFFORT}:
        raise RuntimeError("first Responses request omitted the exact model or xhigh reasoning effort")
    forbidden = sorted({"temperature", "top_p", "stop", "reasoning_effort"} & first.keys())
    if forbidden:
        raise RuntimeError(f"captured request contains unsupported sampling controls: {forbidden}")
    tools = first.get("tools")
    if not isinstance(tools, list) or not tools or any("function" in tool for tool in tools):
        raise RuntimeError("OpenHands function schemas were not flattened for Responses")
    if second.get("previous_response_id") != "resp_local_1":
        raise RuntimeError("second request did not continue the first stored Response")
    if second.get("input") != [{"type": "function_call_output", "call_id": "call_local_1", "output": "/workspace\n"}]:
        raise RuntimeError("second request did not preserve the OpenHands function result call ID")
    return {
        "ok": True,
        "endpoint": "localhost-only-openai-responses",
        "model": first["model"],
        "reasoning_effort": first["reasoning"]["effort"],
        "turns_proved": 2,
        "function_call_id_preserved": True,
        "stored_continuation": True,
        "per_llm_state_isolation": True,
        "forbidden_parameters_absent": ["temperature", "top_p", "stop", "reasoning_effort"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prove(args.repository_root), sort_keys=True))


if __name__ == "__main__":
    main()

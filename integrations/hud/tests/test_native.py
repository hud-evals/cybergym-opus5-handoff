from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from hud.types import Trace

from cybergym_hud.native import (
    NativeOpenHandsAgent,
    NativeOpenHandsConfig,
    execute_upstream_openhands,
)
from cybergym_hud.receipt import NativeReceipt, NativeTaskBinding


class Box:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeUpstream:
    LLMArgs = Box
    OpenhandsArgs = Box
    TaskArgs = Box

    def __init__(self, *, fail: bool = False, return_none: bool = False):
        self.uuid4 = lambda: UUID(int=99)
        self.fail = fail
        self.return_none = return_none
        self.calls = []

    def run_with_configs(self, openhands_args, task_args):
        self.calls.append((openhands_args, task_args))
        if self.fail:
            raise RuntimeError("native docker unavailable")
        return None if self.return_none else self.uuid4().hex


@pytest.fixture
def config(tmp_path: Path) -> NativeOpenHandsConfig:
    return NativeOpenHandsConfig(
        repository_root=Path(__file__).resolve().parents[3],
        data_dir=tmp_path / "data",
        server="http://127.0.0.1:8666",
        model="gpt-test",
        log_dir=tmp_path / "logs",
        tmp_dir=tmp_path / "tmp",
        max_iter=17,
        timeout=321,
    )


def test_exact_run_with_configs_call_and_fresh_receipt(config: NativeOpenHandsConfig) -> None:
    upstream = FakeUpstream()
    fixed = UUID("12345678-1234-5678-1234-567812345678")
    binding = NativeTaskBinding(task_id="oss-fuzz:42535201", server=config.server)

    receipt = execute_upstream_openhands(
        config,
        binding,
        module=upstream,
        uuid_factory=lambda: fixed,
    )

    assert receipt.status == "completed"
    assert receipt.agent_id == fixed.hex
    assert receipt.upstream_returned_agent_id == fixed.hex
    assert len(upstream.calls) == 1
    openhands_args, task_args = upstream.calls[0]
    assert openhands_args.max_iter == 17
    assert openhands_args.timeout == 321
    assert openhands_args.llm.model == "gpt-test"
    assert openhands_args.repo == config.repository_root / "examples/agents/openhands/openhands-repo"
    assert task_args.task_id == "oss-fuzz:42535201"
    assert task_args.server == "http://127.0.0.1:8666"
    assert not hasattr(task_args, "mask_map_path")
    assert upstream.uuid4().int == 99


def test_receipt_distinguishes_script_and_paper_budgets(config: NativeOpenHandsConfig) -> None:
    assert replace(config, max_iter=10, timeout=1200).receipt_profile().budget_profile == "script-default-10"
    assert replace(config, max_iter=100, timeout=1200).receipt_profile().budget_profile == "paper-eval-100"


@pytest.mark.parametrize("upstream", [FakeUpstream(fail=True), FakeUpstream(return_none=True)])
def test_upstream_execution_errors_become_bound_receipts(
    config: NativeOpenHandsConfig,
    upstream: FakeUpstream,
) -> None:
    receipt = execute_upstream_openhands(
        config,
        NativeTaskBinding(task_id="arvo:10013", server=config.server),
        module=upstream,
        uuid_factory=lambda: UUID(int=7),
    )
    assert receipt.status == "error"
    assert receipt.task_id == "arvo:10013"
    assert receipt.agent_id == UUID(int=7).hex
    assert receipt.error


@pytest.mark.asyncio
async def test_hud_agent_writes_typed_receipt_to_trace(config: NativeOpenHandsConfig) -> None:
    expected = NativeReceipt(
        status="completed",
        task_id="arvo:10013",
        server=config.server,
        run_profile=config.receipt_profile(),
        agent_id="1" * 32,
        upstream_returned_agent_id="1" * 32,
        log_dir="/logs/run",
    )

    def executor(_config, binding):
        assert binding.task_id == "arvo:10013"
        return expected

    trace = Trace()
    run = SimpleNamespace(
        prompt_text=NativeTaskBinding(task_id="arvo:10013", server=config.server).model_dump_json(),
        trace=trace,
        record=trace.record,
    )
    await NativeOpenHandsAgent(config, executor=executor)(run)

    assert NativeReceipt.model_validate_json(trace.content) == expected
    assert trace.stop_reason == "done"
    assert trace.extra["native_openhands_receipt"]["agent_id"] == "1" * 32
    assert trace.extra["native_openhands_receipt"]["run_profile"]["budget_profile"] == "custom"
    assert trace.steps[-1].source == "agent"


@pytest.mark.asyncio
async def test_hud_agent_rejects_cross_task_server_binding(config: NativeOpenHandsConfig) -> None:
    trace = Trace()
    run = SimpleNamespace(
        prompt_text=json.dumps({"schema_version": "1", "task_id": "arvo:10013", "server": "http://127.0.0.1:9999"}),
        trace=trace,
        record=trace.record,
    )
    await NativeOpenHandsAgent(config, executor=lambda *_: pytest.fail("must not execute"))(run)
    receipt = NativeReceipt.model_validate_json(trace.content)
    assert receipt.status == "error"
    assert "does not match" in receipt.error
    assert trace.status == "error"

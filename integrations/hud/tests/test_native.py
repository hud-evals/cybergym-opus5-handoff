from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from hud.eval.file_tracking import FileTrackingClient
from hud.types import Trace

from cybergym_hud.env import build_env
from cybergym_hud.native import (
    NativeOpenHandsAgent,
    NativeOpenHandsConfig,
    _classify_error_reasons,
    _controller_termination,
    _OpenHandsSubprocessProxy,
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


def test_parallel_calls_use_independent_upstream_modules(
    monkeypatch: pytest.MonkeyPatch,
    config: NativeOpenHandsConfig,
) -> None:
    barrier = threading.Barrier(2)
    modules: list[FakeUpstream] = []

    class ConcurrentUpstream(FakeUpstream):
        def run_with_configs(self, openhands_args, task_args):
            self.calls.append((openhands_args, task_args))
            barrier.wait(timeout=3)
            return self.uuid4().hex

    def load(_root: Path):
        module = ConcurrentUpstream()
        modules.append(module)
        return module

    monkeypatch.setattr("cybergym_hud.native.validate_contract", lambda **_kwargs: None)
    monkeypatch.setattr("cybergym_hud.native.load_upstream_openhands", load)
    bindings = [
        NativeTaskBinding(task_id="arvo:10013", server=config.server),
        NativeTaskBinding(task_id="oss-fuzz:42535201", server=config.server),
    ]
    uuids = [UUID(int=1), UUID(int=2)]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                execute_upstream_openhands,
                config,
                binding,
                uuid_factory=lambda value=value: value,
            )
            for binding, value in zip(bindings, uuids, strict=True)
        ]
        receipts = [future.result(timeout=5) for future in futures]

    assert [receipt.status for receipt in receipts] == ["completed", "completed"]
    assert [receipt.agent_id for receipt in receipts] == [value.hex for value in uuids]
    assert len({id(module) for module in modules}) == 2
    assert all(module.uuid4().int == 99 for module in modules)


def test_receipt_distinguishes_script_and_paper_budgets(config: NativeOpenHandsConfig) -> None:
    assert replace(config, max_iter=10, timeout=1200).receipt_profile().budget_profile == "script-default-10"
    assert replace(config, max_iter=100, timeout=1200).receipt_profile().budget_profile == "paper-eval-100"


def test_gpt56_xhigh_profile_is_validated_and_receipted(config: NativeOpenHandsConfig) -> None:
    configured = replace(config, model="gpt-5.6-sol", reasoning_effort="xhigh").normalized()
    profile = configured.receipt_profile()
    assert profile.model == "gpt-5.6-sol"
    assert profile.reasoning_effort == "xhigh"
    assert profile.reasoning_transport == "gpt56_openai_responses_bridge"
    assert profile.response_storage == "openai_store_true"
    assert profile.response_continuation == "per_llm_previous_response_id_exact_transcript_extensions"
    assert profile.omitted_sampling_parameters == ("temperature", "top_p", "stop")
    with pytest.raises(ValueError, match="gpt-5.6-sol/xhigh"):
        replace(config, reasoning_effort="xhigh").normalized()


def test_openhands_subprocess_proxy_injects_only_the_exact_child(tmp_path: Path) -> None:
    calls = []

    class Delegate:
        TimeoutExpired = subprocess.TimeoutExpired

        def run(self, command, *args, **kwargs):
            calls.append((command, args, kwargs))
            return "done"

    proxy = _OpenHandsSubprocessProxy(Delegate(), shim_dir=tmp_path / "shim", reasoning_effort="xhigh")
    command = ["/usr/bin/poetry", "run", "python", "-m", "openhands.core.main", "--config-file", "x"]
    assert proxy.run(command, env={"LLM_API_KEY": "secret"}) == "done"
    assert calls[0][2]["env"] == {
        "LLM_API_KEY": "secret",
        "PYTHONPATH": str(tmp_path / "shim"),
        "CYBERGYM_REASONING_EFFORT": "xhigh",
    }
    with pytest.raises(RuntimeError, match="unexpected command"):
        proxy.run(["/usr/bin/poetry", "run", "python", "other.py"], env={})


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


def test_zero_exit_openhands_controller_error_becomes_infra_receipt(config: NativeOpenHandsConfig) -> None:
    class ControllerErrorUpstream(FakeUpstream):
        def run_with_configs(self, openhands_args, task_args):
            agent_id = self.uuid4().hex
            receipt_dir = openhands_args.log_dir / f"{task_args.task_id.replace(':', '_')}-{agent_id}"
            log_dir = receipt_dir / "logs"
            log_dir.mkdir(parents=True)
            (log_dir / "openhands_test.log").write_text(
                "AgentStateChangedObservation(content='', agent_state='error', reason='private provider diagnostic')\n",
                encoding="utf-8",
            )
            return agent_id

    receipt = execute_upstream_openhands(
        config,
        NativeTaskBinding(task_id="arvo:10013", server=config.server),
        module=ControllerErrorUpstream(),
        uuid_factory=lambda: UUID(int=8),
    )

    assert receipt.status == "error"
    assert receipt.upstream_returned_agent_id == UUID(int=8).hex
    assert "error state" in receipt.error
    assert "private provider diagnostic" not in receipt.error


class StructuredControllerUpstream(FakeUpstream):
    def __init__(self, reasons: list[str]):
        super().__init__()
        self.reasons = reasons

    def run_with_configs(self, openhands_args, task_args):
        agent_id = self.uuid4().hex
        receipt_dir = openhands_args.log_dir / f"{task_args.task_id.replace(':', '_')}-{agent_id}"
        events_dir = receipt_dir / "file" / "sessions" / "session" / "events"
        events_dir.mkdir(parents=True)
        for index, reason in enumerate(self.reasons):
            (events_dir / f"{index:04}.json").write_text(
                json.dumps(
                    {
                        "source": "environment",
                        "observation": "agent_state_changed",
                        "extras": {"agent_state": "error", "reason": reason},
                    }
                ),
                encoding="utf-8",
            )
        return agent_id


def test_exact_max_iteration_controller_state_is_canonical_completion(config: NativeOpenHandsConfig) -> None:
    reason = "RuntimeError: Agent reached maximum iteration in headless mode. Current iteration: 17, max iteration: 17"
    receipt = execute_upstream_openhands(
        config,
        NativeTaskBinding(task_id="arvo:10013", server=config.server),
        module=StructuredControllerUpstream([reason]),
        uuid_factory=lambda: UUID(int=9),
    )

    assert receipt.status == "completed"
    assert receipt.error is None
    assert receipt.upstream_returned_agent_id == UUID(int=9).hex


@pytest.mark.parametrize(
    "reasons",
    [
        ["RuntimeError: Agent reached maximum iteration in headless mode. Current iteration: 10, max iteration: 10"],
        ["RuntimeError: Agent reached maximum budget in headless mode. Current budget: 1.50, max budget: 1.50"],
        [
            "RuntimeError: Agent reached maximum iteration in headless mode. Current iteration: 17, max iteration: 17",
            "private provider diagnostic",
        ],
    ],
)
def test_mismatched_or_mixed_controller_errors_remain_infra(
    config: NativeOpenHandsConfig,
    reasons: list[str],
) -> None:
    receipt = execute_upstream_openhands(
        config,
        NativeTaskBinding(task_id="arvo:10013", server=config.server),
        module=StructuredControllerUpstream(reasons),
        uuid_factory=lambda: UUID(int=10),
    )

    assert receipt.status == "error"
    assert "error state" in receipt.error


def test_structured_max_iteration_cannot_mask_later_logged_provider_error(tmp_path: Path) -> None:
    receipt_dir = tmp_path / "run"
    events_dir = receipt_dir / "file" / "sessions" / "session" / "events"
    events_dir.mkdir(parents=True)
    (events_dir / "0000.json").write_text(
        json.dumps(
            {
                "source": "environment",
                "observation": "agent_state_changed",
                "extras": {
                    "agent_state": "error",
                    "reason": (
                        "RuntimeError: Agent reached maximum iteration in headless mode. "
                        "Current iteration: 17, max iteration: 17"
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    logs_dir = receipt_dir / "logs"
    logs_dir.mkdir()
    (logs_dir / "openhands_test.log").write_text(
        "AgentStateChangedObservation(content='', agent_state='error', reason='private provider diagnostic')\n",
        encoding="utf-8",
    )

    assert _controller_termination(receipt_dir, max_iter=17) == "error"


@pytest.mark.parametrize(
    "event",
    [
        [],
        {"source": "environment", "observation": "agent_state_changed", "extras": None},
        {
            "source": "environment",
            "observation": "agent_state_changed",
            "extras": {"agent_state": None, "reason": ""},
        },
    ],
)
def test_malformed_canonical_state_event_fails_closed(tmp_path: Path, event: object) -> None:
    receipt_dir = tmp_path / "run"
    events_dir = receipt_dir / "file" / "sessions" / "session" / "events"
    events_dir.mkdir(parents=True)
    (events_dir / "0000.json").write_text(json.dumps(event), encoding="utf-8")

    assert _controller_termination(receipt_dir, max_iter=17) == "error"


def test_overlong_iteration_sentinel_fails_closed() -> None:
    reason = (
        "RuntimeError: Agent reached maximum iteration in headless mode. Current iteration: "
        + "9" * 5000
        + ", max iteration: "
        + "9" * 5000
    )
    assert _classify_error_reasons([reason], max_iter=17) == "error"


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
async def test_hud_cancellation_waits_for_the_native_worker_lifecycle(
    config: NativeOpenHandsConfig,
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def executor(_config, binding):
        started.set()
        assert release.wait(timeout=5)
        finished.set()
        return NativeReceipt(
            status="completed",
            task_id=binding.task_id,
            server=binding.server,
            run_profile=config.receipt_profile(),
            agent_id="2" * 32,
            upstream_returned_agent_id="2" * 32,
            log_dir="/logs/run",
        )

    trace = Trace()
    run = SimpleNamespace(
        prompt_text=NativeTaskBinding(task_id="arvo:10013", server=config.server).model_dump_json(),
        trace=trace,
        record=trace.record,
    )
    worker_pool = ThreadPoolExecutor(max_workers=1)
    task = asyncio.create_task(NativeOpenHandsAgent(config, executor=executor, worker_pool=worker_pool)(run))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        assert not finished.is_set()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    finally:
        release.set()
        worker_pool.shutdown(wait=True, cancel_futures=True)


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


@pytest.mark.asyncio
async def test_receipt_env_observes_real_upstream_workspace_without_shell(
    config: NativeOpenHandsConfig,
) -> None:
    receipt_env = build_env(file_tracking_root=config.tmp_dir)
    await receipt_env.start()
    client = None
    try:
        assert [capability.name for capability in receipt_env.capabilities] == ["filetracking"]
        capability = receipt_env.capability("filetracking")
        assert capability.protocol == "filetracking/1"
        assert capability.params["root"] == config.tmp_dir.resolve().as_posix()

        client = await FileTrackingClient.connect(capability)
        await client.call("setup")

        # This is the exact directory shape that upstream run_with_configs
        # creates and bind-mounts as /workspace for the OpenHands model.
        model_workspace = config.tmp_dir / f"arvo_10013-{'1' * 32}" / "workspace"
        model_workspace.mkdir(parents=True)
        (model_workspace / "poc").write_bytes(b"tracked exploit input")

        observed = await client.call("diff")
        assert observed["files_changed"] == 1
        assert observed["patches"][0]["path"] == f"arvo_10013-{'1' * 32}/workspace/poc"
        assert observed["patches"][0]["status"] == "added"
    finally:
        if client is not None:
            await client.close()
        await receipt_env.stop()

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
from hud.agents.types import AgentStep
from hud.eval.file_tracking import FileTrackingClient
from hud.telemetry.context import set_trace_context
from hud.types import Trace

from cybergym_hud.env import build_env
from cybergym_hud.native import (
    NativeOpenHandsAgent,
    NativeOpenHandsBatchAgent,
    NativeOpenHandsConfig,
    _classify_error_reasons,
    _controller_termination,
    _OpenHandsSubprocessProxy,
    execute_upstream_openhands,
)
from cybergym_hud.openhands_trace import (
    ProjectedStep,
    TraceImportResult,
    build_trace_import_metadata,
)
from cybergym_hud.receipt import NativeReceipt, NativeTaskBinding


class Box:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _task_args(binding: NativeTaskBinding) -> dict[str, str]:
    return binding.model_dump(exclude={"schema_version"})


def _write_controller_states(openhands_args, task_args, agent_id: str, states: list[tuple[str, str]]) -> None:
    receipt_dir = openhands_args.log_dir / f"{task_args.task_id.replace(':', '_')}-{agent_id}"
    events_dir = receipt_dir / "file" / "sessions" / "session" / "events"
    events_dir.mkdir(parents=True)
    for index, (state, reason) in enumerate(states):
        (events_dir / f"{index:04}.json").write_text(
            json.dumps(
                {
                    "source": "environment",
                    "observation": "agent_state_changed",
                    "extras": {"agent_state": state, "reason": reason},
                }
            ),
            encoding="utf-8",
        )


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
        if self.return_none:
            return None
        agent_id = self.uuid4().hex
        _write_controller_states(openhands_args, task_args, agent_id, [("finished", "")])
        return agent_id


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
            agent_id = self.uuid4().hex
            _write_controller_states(openhands_args, task_args, agent_id, [("finished", "")])
            return agent_id

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


def test_runtime_limits_are_coherent_and_receipted(config: NativeOpenHandsConfig) -> None:
    configured = replace(
        config,
        runtime_nano_cpus=4_000_000_000,
        runtime_memory_bytes=8 * 1024**3,
        runtime_memory_swap_bytes=8 * 1024**3,
    ).normalized()
    profile = configured.receipt_profile()
    assert profile.runtime_nano_cpus == 4_000_000_000
    assert profile.runtime_memory_bytes == 8 * 1024**3
    assert profile.runtime_memory_swap_bytes == 8 * 1024**3
    with pytest.raises(ValueError, match="together"):
        replace(config, runtime_nano_cpus=4_000_000_000).normalized()
    with pytest.raises(ValueError, match="may not be lower"):
        replace(
            config,
            runtime_nano_cpus=4_000_000_000,
            runtime_memory_bytes=8 * 1024**3,
            runtime_memory_swap_bytes=4 * 1024**3,
        ).normalized()


def test_openhands_subprocess_proxy_injects_only_the_exact_child(tmp_path: Path) -> None:
    calls = []

    class Delegate:
        TimeoutExpired = subprocess.TimeoutExpired

        def run(self, command, *args, **kwargs):
            calls.append((command, args, kwargs))
            return "done"

    runtime_kwargs = {
        "auto_remove": True,
        "nano_cpus": 4_000_000_000,
        "mem_limit": 8 * 1024**3,
        "memswap_limit": 8 * 1024**3,
    }
    proxy = _OpenHandsSubprocessProxy(
        Delegate(),
        shim_dir=tmp_path / "shim",
        reasoning_effort="xhigh",
        runtime_kwargs=runtime_kwargs,
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text("[sandbox]\ndocker_runtime_kwargs = {auto_remove = true}\n", encoding="utf-8")
    command = [
        "/usr/bin/poetry",
        "run",
        "python",
        "-m",
        "openhands.core.main",
        "--config-file",
        str(config_path),
    ]
    assert proxy.run(command, env={"LLM_API_KEY": "secret"}) == "done"
    assert calls[0][2]["env"] == {
        "LLM_API_KEY": "secret",
        "PYTHONPATH": str(tmp_path / "shim"),
        "CYBERGYM_REASONING_EFFORT": "xhigh",
    }
    rendered = config_path.read_text(encoding="utf-8")
    assert "nano_cpus = 4000000000" in rendered
    assert "mem_limit = 8589934592" in rendered
    assert "memswap_limit = 8589934592" in rendered
    assert config_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match="unexpected command"):
        proxy.run(["/usr/bin/poetry", "run", "python", "other.py"], env={})


@pytest.mark.asyncio
async def test_batch_prelaunch_verifier_runs_before_native_executor(
    monkeypatch: pytest.MonkeyPatch,
    config: NativeOpenHandsConfig,
) -> None:
    binding = NativeTaskBinding(task_id="arvo:10013", server=config.server)
    events: list[tuple[str, str]] = []

    async def verify(trace_id: str, task_id: str) -> None:
        events.append((trace_id, task_id))

    async def record(*_args, **_kwargs) -> None:
        events.append(("record", "native"))

    monkeypatch.setattr("cybergym_hud.native._run_and_record", record)
    agent = NativeOpenHandsBatchAgent(
        {binding.task_id: config},
        prelaunch_verifier=verify,
    )
    with set_trace_context("a" * 32):
        await agent(SimpleNamespace(prompt_text="model-visible prompt", _args=_task_args(binding)))
    assert events == [("a" * 32, binding.task_id), ("record", "native")]

    async def block(_trace_id: str, _task_id: str) -> None:
        raise RuntimeError("HUD trace missing")

    events.clear()
    blocked = NativeOpenHandsBatchAgent(
        {binding.task_id: config},
        prelaunch_verifier=block,
    )
    with set_trace_context("b" * 32), pytest.raises(RuntimeError, match="HUD trace missing"):
        await blocked(SimpleNamespace(prompt_text="model-visible prompt", _args=_task_args(binding)))
    assert events == []


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


def test_trace_tailer_finalizes_after_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
    config: NativeOpenHandsConfig,
) -> None:
    events: list[str] = []

    class FakeTailer:
        def start(self) -> None:
            events.append("start")

        def finish(self, *, final_projection: bool) -> None:
            events.append(f"finish:{final_projection}")

        def projection_snapshot(self) -> tuple[ProjectedStep, ...]:
            return ()

    monkeypatch.setattr(
        "cybergym_hud.native._make_openhands_trace_tailer",
        lambda _config, _receipt_dir, **_kwargs: FakeTailer(),
    )
    configured = replace(config, trace_step_sink=lambda _step: None)
    receipt = execute_upstream_openhands(
        configured,
        NativeTaskBinding(task_id="arvo:10013", server=config.server),
        module=FakeUpstream(fail=True),
        uuid_factory=lambda: UUID(int=12),
    )

    assert receipt.status == "error"
    assert events == ["start", "finish:False"]


def test_zero_exit_openhands_controller_error_becomes_infra_receipt(config: NativeOpenHandsConfig) -> None:
    class ControllerErrorUpstream(FakeUpstream):
        def run_with_configs(self, openhands_args, task_args):
            agent_id = self.uuid4().hex
            _write_controller_states(
                openhands_args,
                task_args,
                agent_id,
                [("error", "private provider diagnostic")],
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
    assert "canonical gradeable structured terminal state" in receipt.error
    assert "private provider diagnostic" not in receipt.error


class StructuredControllerUpstream(FakeUpstream):
    def __init__(self, states: list[tuple[str, str]]):
        super().__init__()
        self.states = states

    def run_with_configs(self, openhands_args, task_args):
        agent_id = self.uuid4().hex
        _write_controller_states(openhands_args, task_args, agent_id, self.states)
        return agent_id


def _structured_receipt_dir(tmp_path: Path, states: list[tuple[str, str]]) -> Path:
    agent_id = "f" * 32
    _write_controller_states(Box(log_dir=tmp_path), Box(task_id="arvo:10013"), agent_id, states)
    return tmp_path / f"arvo_10013-{agent_id}"


@pytest.mark.parametrize(("state", "classification"), [("finished", "finished"), ("rejected", "rejected")])
def test_structured_finished_and_rejected_are_canonical_completion(
    config: NativeOpenHandsConfig,
    state: str,
    classification: str,
) -> None:
    receipt = execute_upstream_openhands(
        config,
        NativeTaskBinding(task_id="arvo:10013", server=config.server),
        module=StructuredControllerUpstream([(state, "")]),
        uuid_factory=lambda: UUID(int=11),
    )

    assert receipt.status == "completed"
    assert _controller_termination(Path(receipt.log_dir), max_iter=17) == classification


def test_exact_max_iteration_controller_state_is_canonical_completion(config: NativeOpenHandsConfig) -> None:
    reason = "RuntimeError: Agent reached maximum iteration in headless mode. Current iteration: 17, max iteration: 17"
    receipt = execute_upstream_openhands(
        config,
        NativeTaskBinding(task_id="arvo:10013", server=config.server),
        module=StructuredControllerUpstream([("error", reason)]),
        uuid_factory=lambda: UUID(int=9),
    )

    assert receipt.status == "completed"
    assert receipt.error is None
    assert receipt.upstream_returned_agent_id == UUID(int=9).hex


@pytest.mark.parametrize(
    "states",
    [
        [
            (
                "error",
                "RuntimeError: Agent reached maximum iteration in headless mode. "
                "Current iteration: 10, max iteration: 10",
            )
        ],
        [
            (
                "error",
                "RuntimeError: Agent reached maximum budget in headless mode. Current budget: 1.50, max budget: 1.50",
            )
        ],
        [
            (
                "error",
                "RuntimeError: Agent reached maximum iteration in headless mode. "
                "Current iteration: 17, max iteration: 17",
            ),
            ("error", "private provider diagnostic"),
        ],
        [
            (
                "error",
                "RuntimeError: Agent reached maximum iteration in headless mode. "
                "Current iteration: 17, max iteration: 17",
            ),
            ("finished", ""),
        ],
    ],
)
def test_mismatched_or_mixed_controller_errors_remain_infra(
    config: NativeOpenHandsConfig,
    states: list[tuple[str, str]],
) -> None:
    receipt = execute_upstream_openhands(
        config,
        NativeTaskBinding(task_id="arvo:10013", server=config.server),
        module=StructuredControllerUpstream(states),
        uuid_factory=lambda: UUID(int=10),
    )

    assert receipt.status == "error"
    assert "canonical gradeable structured terminal state" in receipt.error


def _canonical_max_iteration_log_line(max_iter: int = 17) -> str:
    reason = (
        "RuntimeError: Agent reached maximum iteration in headless mode. "
        f"Current iteration: {max_iter}, max iteration: {max_iter}"
    )
    return (
        "12:34:56 - openhands:INFO: agent_controller.py:428 - "
        "[Agent Controller 12345678-1234-5678-1234-567812345678] "
        "AgentStateChangedObservation(content='', agent_state='error', "
        f"reason={reason!r}, observation="
        "<ObservationType.AGENT_STATE_CHANGED: 'agent_state_changed'>)\n"
    )


def test_raw_log_never_authorizes_max_iteration_completion(tmp_path: Path) -> None:
    receipt_dir = tmp_path / "run"
    logs_dir = receipt_dir / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "openhands_test.log").write_text(_canonical_max_iteration_log_line(), encoding="utf-8")

    assert _controller_termination(receipt_dir, max_iter=17) == "error"


def test_multiline_cmd_output_cannot_spoof_raw_log_fallback(tmp_path: Path) -> None:
    receipt_dir = tmp_path / "run"
    logs_dir = receipt_dir / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "openhands_test.log").write_text(
        "12:34:55 - openhands:INFO: agent_controller.py:428 - "
        "[Agent Controller 12345678-1234-5678-1234-567812345678] "
        "**CmdOutputObservation (source=EventSource.ENVIRONMENT, exit code=0, metadata={})**\n"
        "--BEGIN AGENT OBSERVATION--\n" + _canonical_max_iteration_log_line() + "--END AGENT OBSERVATION--\n",
        encoding="utf-8",
    )

    assert _controller_termination(receipt_dir, max_iter=17) == "error"


def test_structured_finished_state_is_authoritative_over_spoofed_raw_log(tmp_path: Path) -> None:
    receipt_dir = _structured_receipt_dir(tmp_path, [("finished", "")])
    logs_dir = receipt_dir / "logs"
    logs_dir.mkdir()
    (logs_dir / "openhands_test.log").write_text(_canonical_max_iteration_log_line(), encoding="utf-8")

    assert _controller_termination(receipt_dir, max_iter=17) == "finished"


@pytest.mark.parametrize("state", ["running", "paused", "stopped"])
def test_missing_or_ungradeable_structured_terminal_state_fails_closed(tmp_path: Path, state: str) -> None:
    receipt_dir = _structured_receipt_dir(tmp_path, [(state, "")])

    assert _controller_termination(receipt_dir, max_iter=17) == "error"


def test_missing_structured_event_store_fails_closed(tmp_path: Path) -> None:
    assert _controller_termination(tmp_path / "missing", max_iter=17) == "error"


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
        {
            "source": "environment",
            "observation": "agent_state_changed",
            "extras": {"agent_state": "future_unknown_state", "reason": ""},
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
async def test_hud_agent_writes_typed_receipt_to_trace(
    monkeypatch: pytest.MonkeyPatch,
    config: NativeOpenHandsConfig,
) -> None:
    expected = NativeReceipt(
        status="completed",
        task_id="arvo:10013",
        server=config.server,
        run_profile=config.receipt_profile(),
        agent_id="1" * 32,
        upstream_returned_agent_id="1" * 32,
        log_dir=str(config.log_dir / "run"),
    )

    projected = ProjectedStep(
        "response:fixture",
        AgentStep(content="finished", done=True),
    )

    def executor(runtime_config, binding):
        assert binding.task_id == "arvo:10013"
        assert runtime_config.trace_step_sink is not None
        assert runtime_config.trace_projection_sink is not None
        runtime_config.trace_step_sink(projected.step)
        runtime_config.trace_projection_sink((projected,))
        return expected

    def import_without_workspace_read(*_args, **kwargs):
        assert "workspace_submit" not in kwargs
        return TraceImportResult(
            steps=(projected,),
            metadata=build_trace_import_metadata((projected,), status="completed"),
        )

    monkeypatch.setattr("cybergym_hud.native.import_openhands_trace", import_without_workspace_read)

    trace = Trace()
    run = SimpleNamespace(
        prompt_text="model-visible prompt",
        _args=_task_args(NativeTaskBinding(task_id="arvo:10013", server=config.server)),
        trace=trace,
        record=trace.record,
    )
    await NativeOpenHandsAgent(config, executor=executor)(run)

    assert NativeReceipt.model_validate_json(trace.content) == expected
    assert trace.stop_reason == "done"
    assert trace.extra["native_openhands_receipt"]["agent_id"] == "1" * 32
    assert trace.extra["native_openhands_receipt"]["run_profile"]["budget_profile"] == "custom"
    assert [step.source for step in trace.steps] == ["agent", "system"]
    assert trace.extra["openhands_trace_import"]["projected_step_count"] == 1


@pytest.mark.asyncio
async def test_projected_steps_are_acknowledged_on_the_event_loop_before_receipt(
    monkeypatch: pytest.MonkeyPatch,
    config: NativeOpenHandsConfig,
) -> None:
    loop_thread = threading.get_ident()
    record_threads: list[int] = []
    flush_calls: list[float] = []

    def flush(timeout: float) -> bool:
        flush_calls.append(timeout)
        return True

    monkeypatch.setattr("cybergym_hud.native.flush_telemetry", flush)

    expected = NativeReceipt(
        status="completed",
        task_id="arvo:10013",
        server=config.server,
        run_profile=config.receipt_profile(),
        agent_id="3" * 32,
        upstream_returned_agent_id="3" * 32,
        log_dir="/logs/run",
    )

    def executor(runtime_config, _binding):
        assert runtime_config.trace_step_sink is not None
        assert runtime_config.trace_projection_sink is not None
        projected = ProjectedStep(
            "response:visible",
            AgentStep(content="visible native turn"),
        )
        runtime_config.trace_step_sink(projected.step)
        runtime_config.trace_projection_sink((projected,))
        # The sink blocks until run.record completed on the loop thread.
        assert len(trace.steps) == 1
        return expected

    trace = Trace()

    def record(step):
        record_threads.append(threading.get_ident())
        trace.record(step)

    run = SimpleNamespace(
        prompt_text=NativeTaskBinding(task_id="arvo:10013", server=config.server).model_dump_json(),
        _args=_task_args(NativeTaskBinding(task_id="arvo:10013", server=config.server)),
        trace=trace,
        record=record,
    )
    await NativeOpenHandsAgent(config, executor=executor)(run)

    assert trace.steps[0].content == "visible native turn"
    assert all(thread_id == loop_thread for thread_id in record_threads)
    assert flush_calls == [30.0, 30.0]


@pytest.mark.asyncio
async def test_error_receipt_keeps_reconciled_live_prefix_without_saved_trajectory(
    monkeypatch: pytest.MonkeyPatch,
    config: NativeOpenHandsConfig,
) -> None:
    monkeypatch.setattr("cybergym_hud.native.flush_telemetry", lambda _timeout: True)
    monkeypatch.setattr(
        "cybergym_hud.native.import_openhands_trace",
        lambda *_args, **_kwargs: pytest.fail("no saved trajectory should be imported"),
    )
    projected = ProjectedStep(
        "agent-message:7",
        AgentStep(content="validated partial assistant turn"),
    )

    def executor(runtime_config, binding):
        assert runtime_config.trace_step_sink is not None
        assert runtime_config.trace_projection_sink is not None
        runtime_config.trace_step_sink(projected.step)
        runtime_config.trace_projection_sink((projected,))
        return NativeReceipt(
            status="error",
            task_id=binding.task_id,
            server=binding.server,
            run_profile=config.receipt_profile(),
            agent_id="5" * 32,
            log_dir=str(config.log_dir / "partial-run"),
            error="controller interrupted",
        )

    trace = Trace()
    run = SimpleNamespace(
        _args=_task_args(NativeTaskBinding(task_id="arvo:10013", server=config.server)),
        trace=trace,
        record=trace.record,
    )
    await NativeOpenHandsAgent(config, executor=executor)(run)

    assert [step.source for step in trace.steps] == ["agent", "system"]
    metadata = trace.extra["openhands_trace_import"]
    assert metadata["status"] == "partial_error"
    assert metadata["projected_step_count"] == 1
    assert metadata["saved_trajectory_reconciled"] is False
    assert trace.status == "error"


@pytest.mark.asyncio
async def test_false_telemetry_flush_changes_completion_to_error(
    monkeypatch: pytest.MonkeyPatch,
    config: NativeOpenHandsConfig,
) -> None:
    monkeypatch.setattr("cybergym_hud.native.flush_telemetry", lambda _timeout: False)
    expected = NativeReceipt(
        status="completed",
        task_id="arvo:10013",
        server=config.server,
        run_profile=config.receipt_profile(),
        agent_id="4" * 32,
        upstream_returned_agent_id="4" * 32,
        log_dir="/logs/run",
    )
    trace = Trace()
    run = SimpleNamespace(
        prompt_text=NativeTaskBinding(task_id="arvo:10013", server=config.server).model_dump_json(),
        _args=_task_args(NativeTaskBinding(task_id="arvo:10013", server=config.server)),
        trace=trace,
        record=trace.record,
    )

    await NativeOpenHandsAgent(config, executor=lambda *_args: expected)(run)

    receipt = NativeReceipt.model_validate_json(trace.content)
    assert receipt.status == "error"
    assert "telemetry did not flush" in receipt.error
    assert trace.status == "error"


@pytest.mark.asyncio
async def test_hud_cancellation_waits_for_the_native_worker_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    config: NativeOpenHandsConfig,
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    flushed = threading.Event()

    def flush(_timeout: float) -> bool:
        flushed.set()
        return True

    monkeypatch.setattr("cybergym_hud.native.flush_telemetry", flush)

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
        prompt_text="model-visible prompt",
        _args=_task_args(NativeTaskBinding(task_id="arvo:10013", server=config.server)),
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
        assert flushed.is_set()
    finally:
        release.set()
        worker_pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_hud_agent_rejects_cross_task_server_binding(config: NativeOpenHandsConfig) -> None:
    trace = Trace()
    run = SimpleNamespace(
        prompt_text="model-visible prompt",
        _args={"task_id": "arvo:10013", "server": "http://127.0.0.1:9999"},
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

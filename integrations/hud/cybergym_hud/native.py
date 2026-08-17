"""Exact upstream OpenHands 0.33 invocation wrapped as a HUD receipt agent."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import stat
import threading
import tomllib
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Executor, Future
from contextvars import copy_context
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Literal
from uuid import UUID, uuid4

import tomli_w
from cybergym.task.types import generate_agent_id_and_checksum
from hud.agents.base import Agent
from hud.telemetry import flush as flush_telemetry
from hud.telemetry.context import get_current_trace_id
from hud.types import Step

from .contract import OG_PROMPT, openhands_system_prompt, repository_root, validate_contract
from .openhands_trace import (
    ProjectedStep,
    build_trace_import_metadata,
    import_openhands_trace,
    runtime_secret_values,
)
from .receipt import NativeReceipt, NativeRunProfile, NativeTaskBinding, normalize_server
from .runtime_network import RUNTIME_NETWORK_NAME
from .trace_tail import OpenHandsEventTailer, OpenHandsTraceError, SavedTrajectoryProjection
from .upstream import require_upstream_agent_checkout

CAMPAIGN_RUNTIME_NANO_CPUS = 4_000_000_000
CAMPAIGN_RUNTIME_MEMORY_BYTES = 8 * 1024**3
CAMPAIGN_RUNTIME_MEMORY_SWAP_BYTES = CAMPAIGN_RUNTIME_MEMORY_BYTES

_MAX_ITERATION_REASON = re.compile(
    r"^RuntimeError: Agent reached maximum iteration in headless mode\. "
    r"Current iteration: (?P<current>[0-9]+), max iteration: (?P<maximum>[0-9]+)$"
)


@dataclass(frozen=True, slots=True)
class NativeOpenHandsConfig:
    repository_root: Path
    data_dir: Path
    server: str
    model: str
    log_dir: Path
    tmp_dir: Path
    reasoning_effort: Literal["xhigh"] | None = None
    max_iter: int = 10
    timeout: int = 1200
    llm_api_key: str | None = None
    base_url: str = ""
    grader_server_mode: Literal["images", "binary"] = "images"
    native_tool_calling: bool | None = None
    top_p: float = 1.0
    temperature: float = 0.0
    max_output_tokens: int = 2048
    seed: int | None = None
    silent: bool = False
    remove_tmp: bool = True
    debug: bool = False
    runtime_nano_cpus: int | None = None
    runtime_memory_bytes: int | None = None
    runtime_memory_swap_bytes: int | None = None
    runtime_network: Literal["cybergym-no-internet"] = RUNTIME_NETWORK_NAME
    # Runtime-only bridge.  It is deliberately absent from repr/equality and
    # every durable receipt/profile so callable state and trace credentials
    # can never enter campaign journals.
    trace_step_sink: Callable[[Step], None] | None = field(default=None, repr=False, compare=False)
    trace_projection_sink: Callable[[tuple[ProjectedStep, ...]], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def normalized(self) -> NativeOpenHandsConfig:
        root = repository_root(self.repository_root)
        if self.max_iter < 1:
            raise ValueError("max_iter must be at least one")
        if self.timeout < 1:
            raise ValueError("timeout must be at least one second")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if self.grader_server_mode not in {"images", "binary"}:
            raise ValueError("grader_server_mode must be images or binary")
        if self.reasoning_effort is not None and (self.model != "gpt-5.6-sol" or self.reasoning_effort != "xhigh"):
            raise ValueError("reasoning_effort is supported only as gpt-5.6-sol/xhigh")
        runtime_limits = (
            self.runtime_nano_cpus,
            self.runtime_memory_bytes,
            self.runtime_memory_swap_bytes,
        )
        if any(value is not None for value in runtime_limits):
            if not all(isinstance(value, int) and value > 0 for value in runtime_limits):
                raise ValueError("runtime CPU, memory, and memory+swap limits must be positive integers together")
            memory_bytes = self.runtime_memory_bytes
            memory_swap_bytes = self.runtime_memory_swap_bytes
            if not isinstance(memory_bytes, int) or not isinstance(memory_swap_bytes, int):
                raise ValueError("runtime CPU, memory, and memory+swap limits must be positive integers together")
            if memory_swap_bytes < memory_bytes:
                raise ValueError("runtime memory+swap limit may not be lower than the memory limit")
        if self.runtime_network != RUNTIME_NETWORK_NAME:
            raise ValueError(f"unsupported runtime network: {self.runtime_network!r}")
        return NativeOpenHandsConfig(
            repository_root=root,
            data_dir=self.data_dir.expanduser().resolve(),
            server=normalize_server(self.server),
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            log_dir=self.log_dir.expanduser().resolve(),
            tmp_dir=self.tmp_dir.expanduser().resolve(),
            max_iter=self.max_iter,
            timeout=self.timeout,
            llm_api_key=self.llm_api_key,
            base_url=self.base_url,
            grader_server_mode=self.grader_server_mode,
            native_tool_calling=self.native_tool_calling,
            top_p=self.top_p,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            seed=self.seed,
            silent=self.silent,
            remove_tmp=self.remove_tmp,
            debug=self.debug,
            runtime_nano_cpus=self.runtime_nano_cpus,
            runtime_memory_bytes=self.runtime_memory_bytes,
            runtime_memory_swap_bytes=self.runtime_memory_swap_bytes,
            runtime_network=self.runtime_network,
            trace_step_sink=self.trace_step_sink,
            trace_projection_sink=self.trace_projection_sink,
        )

    def receipt_profile(self) -> NativeRunProfile:
        if self.max_iter == 100 and self.timeout == 1200:
            budget_profile = "paper-eval-100"
        elif self.max_iter == 10 and self.timeout == 1200:
            budget_profile = "script-default-10"
        else:
            budget_profile = "custom"
        return NativeRunProfile(
            budget_profile=budget_profile,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            reasoning_transport=("gpt56_openai_responses_bridge" if self.reasoning_effort else "none"),
            response_storage=("openai_store_true" if self.reasoning_effort else "none"),
            response_continuation=(
                "per_llm_previous_response_id_exact_transcript_extensions" if self.reasoning_effort else "none"
            ),
            omitted_sampling_parameters=("temperature", "top_p", "stop") if self.reasoning_effort else (),
            max_iter=self.max_iter,
            timeout_seconds=self.timeout,
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            seed=self.seed,
            native_tool_calling=self.native_tool_calling,
            base_url_mode="custom" if self.base_url else "provider-default",
            grader_server_mode=self.grader_server_mode,
            runtime_nano_cpus=self.runtime_nano_cpus,
            runtime_memory_bytes=self.runtime_memory_bytes,
            runtime_memory_swap_bytes=self.runtime_memory_swap_bytes,
            network_mode="cybergym-docker-internal-no-public-egress-v1",
        )


def load_upstream_openhands(root: Path) -> ModuleType:
    """Load one private copy of the pinned upstream runner for one rollout."""

    agents = require_upstream_agent_checkout(root)
    script = agents / "openhands/run.py"
    name = f"_cybergym_upstream_openhands_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load pinned upstream OpenHands runner at {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_STUCK_IN_LOOP_REASON = "AgentStuckInLoopError: Agent got stuck in a loop"


def _classify_error_reasons(
    reasons: list[str],
    *,
    max_iter: int,
) -> Literal["none", "max_iterations", "stuck_loop", "error"]:
    if not reasons:
        return "none"
    if all(reason == _STUCK_IN_LOOP_REASON for reason in reasons):
        return "stuck_loop"
    for reason in reasons:
        match = _MAX_ITERATION_REASON.fullmatch(reason)
        if match is None:
            return "error"
        try:
            current = int(match.group("current"))
            maximum = int(match.group("maximum"))
        except ValueError:
            return "error"
        if current != max_iter or maximum != max_iter:
            return "error"
    return "max_iterations"


def _controller_termination(
    receipt_log_dir: Path,
    *,
    max_iter: int,
) -> Literal["finished", "rejected", "max_iterations", "stuck_loop", "error"]:
    """Classify pinned OpenHands' zero-exit controller terminal state.

    OpenHands 0.33 represents normal headless iteration exhaustion as
    ``AgentState.ERROR``.  Original CyberGym nevertheless accepts any saved
    trajectory and grades it. Preserve that behavior for the exact configured
    max-iteration sentinel and pinned loop-detector sentinel; every other
    controller terminal state remains a non-reportable infrastructure failure.
    The append-only event store is the only authority: raw logs include
    agent-controlled command output and therefore cannot authorize completion.
    """

    events_root = receipt_log_dir / "file" / "sessions"
    try:
        event_paths = sorted(events_root.glob("*/events/*.json")) if events_root.is_dir() else []
    except OSError:
        return "error"
    if not event_paths:
        return "error"

    terminal_states: list[tuple[str, str]] = []
    try:
        for event_path in event_paths:
            if not stat.S_ISREG(event_path.stat(follow_symlinks=False).st_mode):
                return "error"
            event = json.loads(event_path.read_text(encoding="utf-8"))
            if not isinstance(event, dict):
                return "error"
            if event.get("observation") != "agent_state_changed":
                continue
            extras = event.get("extras")
            if event.get("source") != "environment" or not isinstance(extras, dict):
                return "error"
            agent_state = extras.get("agent_state")
            reason = extras.get("reason")
            if not isinstance(agent_state, str) or not isinstance(reason, str):
                return "error"
            if agent_state in {"finished", "rejected", "error", "paused", "stopped"}:
                terminal_states.append((agent_state, reason))
            elif (
                agent_state
                not in {
                    "loading",
                    "running",
                    "awaiting_user_input",
                    "awaiting_user_confirmation",
                    "user_confirmed",
                    "user_rejected",
                    "rate_limited",
                }
                or reason
            ):
                # Pinned OpenHands attaches a reason only to ERROR. Reject a
                # malformed, unknown, or otherwise noncanonical state rather
                # than silently ignoring controller drift or diagnostics.
                return "error"
    except (OSError, ValueError, TypeError):
        return "error"

    if not terminal_states:
        return "error"

    error_reasons = [reason for state, reason in terminal_states if state == "error"]
    if error_reasons:
        # An ERROR is terminal in this headless runner. Any mixture with a
        # different terminal state is malformed. Only the exact configured
        # iteration limit and pinned model-loop detector are gradeable; all
        # other reasons remain provider/transport/controller infrastructure
        # failures.
        if len(error_reasons) != len(terminal_states):
            return "error"
        return _classify_error_reasons(error_reasons, max_iter=max_iter)

    if len(terminal_states) != 1:
        return "error"
    terminal_state, reason = terminal_states[0]
    if terminal_state in {"finished", "rejected"} and not reason:
        return terminal_state
    return "error"


class _OpenHandsSubprocessProxy:
    """Inject compatibility only into the exact pinned OpenHands child."""

    def __init__(
        self,
        delegate: ModuleType,
        *,
        shim_dir: Path,
        reasoning_effort: str | None,
        runtime_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._delegate = delegate
        self._shim_dir = shim_dir
        self._reasoning_effort = reasoning_effort
        self._runtime_kwargs = runtime_kwargs

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def run(self, command: Any, *args: Any, **kwargs: Any) -> Any:
        normalized = tuple(str(part) for part in command) if isinstance(command, list | tuple) else ()
        marker = ("run", "python", "-m", "openhands.core.main")
        if len(normalized) < 5 or tuple(normalized[1:5]) != marker:
            raise RuntimeError(f"refusing to inject reasoning shim into unexpected command: {normalized!r}")
        child_env = dict(kwargs.get("env") or {})
        if self._reasoning_effort or self._runtime_kwargs is not None:
            child_env["PYTHONPATH"] = str(self._shim_dir)
        if self._reasoning_effort:
            child_env["CYBERGYM_REASONING_EFFORT"] = self._reasoning_effort
        if self._runtime_kwargs is not None:
            child_env["CYBERGYM_RUNTIME_NETWORK"] = str(self._runtime_kwargs["network"])
            try:
                config_index = normalized.index("--config-file") + 1
                config_path = Path(normalized[config_index])
                config = tomllib.loads(config_path.read_text(encoding="utf-8"))
                observed = config.get("sandbox", {}).get("docker_runtime_kwargs")
                if observed != {"auto_remove": True}:
                    raise RuntimeError(f"unexpected pinned Docker runtime defaults: {observed!r}")
                config["sandbox"]["docker_runtime_kwargs"] = self._runtime_kwargs
                encoded = tomli_w.dumps(config).encode()
                temporary = config_path.with_name(f".{config_path.name}.campaign.tmp")
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    remaining = memoryview(encoded)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError("short write while enforcing Docker runtime limits")
                        remaining = remaining[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.replace(temporary, config_path)
                directory = os.open(config_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
                raise RuntimeError("could not enforce paid campaign Docker runtime limits") from exc
        kwargs["env"] = child_env
        return self._delegate.run(command, *args, **kwargs)


def _trace_redactions(
    config: NativeOpenHandsConfig,
    *,
    exact: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Collect exact secrets that could plausibly reach the child/event store."""

    candidates = [
        config.llm_api_key,
        config.base_url,
        *exact,
        *runtime_secret_values(),
    ]
    return tuple(value for value in candidates if value)


def _make_openhands_trace_tailer(
    config: NativeOpenHandsConfig,
    receipt_log_dir: Path,
    *,
    binding: NativeTaskBinding,
    run_profile: NativeRunProfile,
    agent_id: str,
    exact_redactions: tuple[str, ...],
) -> OpenHandsEventTailer:
    """Build the live bridge lazily so direct receipt-only calls stay cheap."""

    if config.trace_step_sink is None:
        raise ValueError("OpenHands trace tailer requires a HUD step sink")
    from .openhands_trace import OpenHandsEventProjector

    redactions = _trace_redactions(config, exact=exact_redactions)

    def load_saved_projection(final: bool) -> SavedTrajectoryProjection:
        # The pinned saved ``trajectory`` omits browser DOM, accessibility
        # trees, and screenshots.  It is therefore the only permitted fallback
        # when a raw live event exceeds the bounded event-file reader.
        receipt = NativeReceipt(
            status="completed" if final else "error",
            task_id=binding.task_id,
            server=binding.server,
            run_profile=run_profile,
            agent_id=agent_id,
            upstream_returned_agent_id=agent_id if final else None,
            log_dir=str(receipt_log_dir),
            error=None if final else "private upstream rollout error",
        )
        imported = import_openhands_trace(
            receipt,
            redactions=redactions,
        )
        return SavedTrajectoryProjection(
            steps=imported.steps,
            source_event_ids=imported.source_event_ids,
        )

    return OpenHandsEventTailer(
        receipt_log_dir,
        projector=OpenHandsEventProjector(redactions=redactions),
        sink=config.trace_step_sink,
        saved_projection_loader=load_saved_projection,
        project_kwargs={
            "origin": "event_store",
            "skip_initial_user_prompt": OG_PROMPT,
        },
    )


def execute_upstream_openhands(
    config: NativeOpenHandsConfig,
    binding: NativeTaskBinding,
    *,
    module: ModuleType | Any | None = None,
    uuid_factory: Callable[[], UUID] = uuid4,
) -> NativeReceipt:
    """Call the pinned module's run_with_configs and return its durable identity."""

    config = config.normalized()
    run_profile = config.receipt_profile()
    if binding.server != config.server:
        return NativeReceipt(
            status="error",
            task_id=binding.task_id,
            server=binding.server,
            run_profile=run_profile,
            error="scheduled server does not match native runner configuration",
        )
    validate_contract(root=config.repository_root)
    upstream = module or load_upstream_openhands(config.repository_root)
    original_subprocess = getattr(upstream, "subprocess", None)
    runtime_kwargs = None
    if config.runtime_nano_cpus is not None or config.runtime_network:
        runtime_kwargs = {"auto_remove": True}
        runtime_kwargs["network"] = config.runtime_network
        if config.runtime_nano_cpus is not None:
            runtime_kwargs.update(
                {
                    "nano_cpus": config.runtime_nano_cpus,
                    "mem_limit": config.runtime_memory_bytes,
                    "memswap_limit": config.runtime_memory_swap_bytes,
                }
            )
    if config.reasoning_effort or runtime_kwargs is not None:
        if original_subprocess is None:
            return NativeReceipt(
                status="error",
                task_id=binding.task_id,
                server=binding.server,
                run_profile=run_profile,
                error="pinned upstream runner has no subprocess transport seam",
            )
        shim_dir = config.repository_root / "integrations/hud/openhands_shim"
        if not (shim_dir / "sitecustomize.py").is_file():
            return NativeReceipt(
                status="error",
                task_id=binding.task_id,
                server=binding.server,
                run_profile=run_profile,
                error=f"OpenHands reasoning compatibility shim is missing: {shim_dir}",
            )
        upstream.subprocess = _OpenHandsSubprocessProxy(
            original_subprocess,
            shim_dir=shim_dir,
            reasoning_effort=config.reasoning_effort,
            runtime_kwargs=runtime_kwargs,
        )
    fresh_uuid = uuid_factory()
    agent_id = fresh_uuid.hex
    generated_agent_id, checksum = generate_agent_id_and_checksum(
        binding.task_id,
        agent_id=agent_id,
    )
    if generated_agent_id != agent_id:
        return NativeReceipt(
            status="error",
            task_id=binding.task_id,
            server=binding.server,
            run_profile=run_profile,
            agent_id=agent_id,
            error="CyberGym generated a different native agent identity",
        )
    run_name = f"{binding.task_id.replace(':', '_')}-{agent_id}"
    receipt_log_dir = config.log_dir / run_name

    llm_args = upstream.LLMArgs(
        model=config.model,
        api_key=config.llm_api_key,
        base_url=config.base_url,
        native_tool_calling=config.native_tool_calling,
        top_p=config.top_p,
        temperature=config.temperature,
        max_output_tokens=config.max_output_tokens,
        seed=config.seed,
    )
    openhands_args = upstream.OpenhandsArgs(
        log_dir=config.log_dir,
        tmp_dir=config.tmp_dir,
        llm=llm_args,
        max_iter=config.max_iter,
        repo=config.repository_root / "examples/agents/openhands/openhands-repo",
        silent=config.silent,
        remove_tmp=config.remove_tmp,
        timeout=config.timeout,
        debug=config.debug,
    )
    task_args = upstream.TaskArgs(
        task_id=binding.task_id,
        data_dir=config.data_dir,
        server=binding.server,
    )

    # run_with_configs owns UUID creation but returns None when trajectory
    # validation fails. Production calls load a private module per rollout, so
    # this injection never mutates state shared with another rollout and does
    # not serialize the expensive native OpenHands executions.
    original_uuid4 = upstream.uuid4
    upstream.uuid4 = lambda: fresh_uuid
    returned: str | None = None
    upstream_error: Exception | None = None
    trace_setup_failed = False
    trace_projection_failed = False
    trace_tailer: OpenHandsEventTailer | None = None
    trace_tailer_started = False
    try:
        if config.trace_step_sink is not None:
            try:
                trace_tailer = _make_openhands_trace_tailer(
                    config,
                    receipt_log_dir,
                    binding=binding,
                    run_profile=run_profile,
                    agent_id=agent_id,
                    exact_redactions=(agent_id, checksum, binding.server),
                )
                trace_tailer.start()
                trace_tailer_started = True
            except Exception:
                trace_setup_failed = True
        if not trace_setup_failed:
            try:
                returned = upstream.run_with_configs(openhands_args, task_args)
            except Exception as exc:
                upstream_error = exc
    finally:
        if trace_tailer is not None and trace_tailer_started:
            try:
                # This is also the final full reconciliation.  It runs after
                # upstream cleanup on success, exception, timeout, and the
                # cancellation path (which waits for this worker to return).
                trace_tailer.finish(
                    final_projection=upstream_error is None and returned == agent_id,
                )
                if config.trace_projection_sink is not None:
                    snapshot = trace_tailer.projection_snapshot()
                    if not all(isinstance(item, ProjectedStep) for item in snapshot):
                        raise OpenHandsTraceError("OpenHands final projection snapshot is malformed")
                    config.trace_projection_sink(snapshot)
            except Exception:
                trace_projection_failed = True
        upstream.uuid4 = original_uuid4
        if config.reasoning_effort or runtime_kwargs is not None:
            upstream.subprocess = original_subprocess

    if trace_setup_failed:
        return NativeReceipt(
            status="error",
            task_id=binding.task_id,
            server=binding.server,
            run_profile=run_profile,
            agent_id=agent_id,
            log_dir=str(receipt_log_dir),
            error="OpenHands HUD trajectory projection could not start; inspect the private rollout log",
        )
    if upstream_error is not None:
        detail = " and HUD trajectory projection failed" if trace_projection_failed else ""
        return NativeReceipt(
            status="error",
            task_id=binding.task_id,
            server=binding.server,
            run_profile=run_profile,
            agent_id=agent_id,
            log_dir=str(receipt_log_dir),
            error=f"upstream run_with_configs failed{detail}; inspect the private rollout log",
        )
    if trace_projection_failed:
        return NativeReceipt(
            status="error",
            task_id=binding.task_id,
            server=binding.server,
            run_profile=run_profile,
            agent_id=agent_id,
            upstream_returned_agent_id=returned,
            log_dir=str(receipt_log_dir),
            error="OpenHands HUD trajectory projection failed; inspect the private rollout log",
        )

    if returned != agent_id:
        return NativeReceipt(
            status="error",
            task_id=binding.task_id,
            server=binding.server,
            run_profile=run_profile,
            agent_id=agent_id,
            upstream_returned_agent_id=returned,
            log_dir=str(receipt_log_dir),
            error="upstream run_with_configs did not produce a valid trajectory receipt",
        )
    controller_termination = _controller_termination(receipt_log_dir, max_iter=config.max_iter)
    if controller_termination == "error":
        return NativeReceipt(
            status="error",
            task_id=binding.task_id,
            server=binding.server,
            run_profile=run_profile,
            agent_id=agent_id,
            upstream_returned_agent_id=returned,
            log_dir=str(receipt_log_dir),
            error=(
                "pinned OpenHands controller did not produce a canonical gradeable structured terminal state; "
                "inspect the private rollout log"
            ),
        )
    return NativeReceipt(
        status="completed",
        task_id=binding.task_id,
        server=binding.server,
        run_profile=run_profile,
        agent_id=agent_id,
        upstream_returned_agent_id=returned,
        log_dir=str(receipt_log_dir),
        controller_termination=controller_termination,
    )


def _threadsafe_trace_sink(run: Any, loop: asyncio.AbstractEventLoop) -> Callable[[Step], None]:
    """Marshal tailer-thread records onto the active HUD trace context."""

    loop_thread_id = threading.get_ident()
    trace_context = copy_context()

    def sink(step: Step) -> None:
        if not isinstance(step, Step):
            raise TypeError("OpenHands trajectory sink requires a HUD Step")
        if threading.get_ident() == loop_thread_id:
            run.record(step)
            return

        acknowledged: Future[None] = Future()

        def record() -> None:
            if not acknowledged.set_running_or_notify_cancel():
                return
            try:
                run.record(step)
            except BaseException as exc:
                acknowledged.set_exception(exc)
            else:
                acknowledged.set_result(None)

        loop.call_soon_threadsafe(record, context=trace_context.copy())
        try:
            # Recording is synchronous.  A timeout prevents an orphaned
            # worker if the owning event loop has stopped servicing callbacks.
            acknowledged.result(timeout=30.0)
        except TimeoutError as exc:
            acknowledged.cancel()
            raise RuntimeError("HUD did not acknowledge a projected OpenHands step") from exc

    return sink


def _telemetry_error_receipt(receipt: NativeReceipt) -> NativeReceipt:
    payload = receipt.model_dump(mode="python")
    payload.update(
        status="error",
        error="HUD telemetry did not flush projected OpenHands steps",
    )
    return NativeReceipt.model_validate(payload)


async def _run_and_record(
    run: Any,
    config: NativeOpenHandsConfig,
    executor: Callable[[NativeOpenHandsConfig, NativeTaskBinding], NativeReceipt],
    worker_pool: Executor | None = None,
) -> None:
    """Execute one bound native rollout and attach its typed HUD receipt."""

    live_projections: list[tuple[ProjectedStep, ...]] = []

    def capture_projection(steps: tuple[ProjectedStep, ...]) -> None:
        if live_projections:
            raise RuntimeError("native executor returned more than one final trace projection")
        if not all(isinstance(item, ProjectedStep) for item in steps):
            raise TypeError("native executor returned a malformed final trace projection")
        live_projections.append(steps)

    try:
        task_args = getattr(run, "_args", None)
        if not isinstance(task_args, Mapping):
            raise ValueError("HUD run does not expose the bound task setup arguments")
        binding = NativeTaskBinding.model_validate({"schema_version": "1", **task_args})
        if binding.server != config.server:
            raise ValueError("HUD task server does not match native runner configuration")
        loop = asyncio.get_running_loop()
        runtime_config = replace(
            config,
            trace_step_sink=_threadsafe_trace_sink(run, loop),
            trace_projection_sink=capture_projection,
        )
        worker = loop.run_in_executor(worker_pool, executor, runtime_config, binding)
        try:
            # Shield the native call so cancelling the HUD coroutine cannot
            # release its rollout slot while OpenHands/Docker is still alive.
            receipt = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Python cannot stop a running worker thread. Keep this rollout
            # alive until the upstream timeout/cleanup path has really ended;
            # only then may Taskset release the semaphore and its runtime.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            flush_worker = loop.run_in_executor(None, flush_telemetry, 30.0)
            while not flush_worker.done():
                try:
                    await asyncio.shield(flush_worker)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            raise
    except Exception as exc:
        task_id = "arvo:invalid"
        server = config.server
        try:
            raw = getattr(run, "_args", None)
            if isinstance(raw, dict):
                task_id = str(raw.get("task_id", task_id))
                server = str(raw.get("server", server))
            receipt = NativeReceipt(
                status="error",
                task_id=task_id,
                server=server,
                run_profile=config.receipt_profile(),
                error=f"native scheduler failed: {type(exc).__name__}: {exc}",
            )
        except Exception:
            receipt = NativeReceipt(
                status="error",
                task_id="arvo:invalid",
                server=config.server,
                run_profile=config.receipt_profile(),
                error=f"native scheduler failed: {type(exc).__name__}: {exc}",
            )

    projection_receipt = receipt
    try:
        flushed = await asyncio.to_thread(flush_telemetry, 30.0)
    except Exception:
        flushed = False
    if not flushed:
        receipt = _telemetry_error_receipt(receipt)

    import_metadata: dict[str, Any] | None = None
    trajectory_exists = bool(projection_receipt.log_dir and (Path(projection_receipt.log_dir) / "trajectory").exists())
    if projection_receipt.status == "completed" or trajectory_exists or live_projections:
        try:
            if len(live_projections) != 1:
                raise RuntimeError("native executor did not return one final live trace projection")
            if projection_receipt.log_dir is None:
                raise RuntimeError("native receipt has no OpenHands log directory")
            receipt_log_dir = Path(projection_receipt.log_dir).expanduser().resolve()
            try:
                receipt_log_dir.relative_to(config.log_dir.expanduser().resolve())
            except ValueError as exc:
                raise RuntimeError("native receipt log directory escaped the configured log root") from exc
            live_steps = live_projections[0]
            live_metadata = build_trace_import_metadata(
                live_steps,
                status=("completed" if projection_receipt.status == "completed" else "partial_error"),
            )
            live_metadata["saved_trajectory_reconciled"] = trajectory_exists
            if trajectory_exists:
                # The OpenHands args already bind task_id and exact-redact
                # server, agent_id, and checksum.  The runtime workspace is correctly
                # root-owned and can be unreadable by the unprivileged host
                # controller, so do not make transcript reconciliation depend
                # on rereading its generated submit.sh.
                imported = import_openhands_trace(
                    projection_receipt,
                    redactions=(
                        *runtime_secret_values(),
                        *(value for value in (config.llm_api_key,) if value),
                    ),
                )
                if tuple(item.key for item in live_steps) != tuple(item.key for item in imported.steps):
                    raise RuntimeError("live and saved OpenHands projections disagree on step identities")
                for digest_name in ("projected_steps_sha256", "projected_events_sha256"):
                    if live_metadata[digest_name] != imported.metadata.get(digest_name):
                        raise RuntimeError(f"live and saved OpenHands projections disagree on {digest_name}")
            import_metadata = live_metadata
        except Exception as exc:
            diagnostic = f"{type(exc).__name__}: OpenHands trajectory import failed; inspect private rollout logs"
            import_metadata = {
                "schema_version": "1",
                "status": "error",
                "error": diagnostic,
            }
    elif projection_receipt.status == "error":
        import_metadata = {
            "schema_version": "1",
            "status": "unavailable_native_error",
            "error": "native rollout produced no saved OpenHands trajectory",
        }
    payload = receipt.model_dump(mode="json")
    run.trace.content = receipt.model_dump_json()
    run.trace.extra.update(
        {
            "runner": receipt.runner,
            "native_openhands_receipt": payload,
            "agent_config": {"system_prompt": openhands_system_prompt(config.repository_root)},
            "openhands_trace_import": import_metadata,
        }
    )
    # A receipt is system infrastructure metadata, never an assistant turn.
    run.record(
        Step(
            source="system",
            extra={
                "native_openhands_receipt": payload,
                "openhands_trace_import": import_metadata,
            },
        )
    )
    try:
        receipt_flushed = await asyncio.to_thread(flush_telemetry, 30.0)
    except Exception:
        receipt_flushed = False
    if not receipt_flushed:
        receipt = _telemetry_error_receipt(receipt)
        payload = receipt.model_dump(mode="json")
        run.trace.content = receipt.model_dump_json()
        run.trace.extra["native_openhands_receipt"] = payload
    run.trace.stop_reason = "done"
    if receipt.status == "error" or not import_metadata or import_metadata.get("status") != "completed":
        run.trace.status = "error"


class NativeOpenHandsAgent(Agent):
    """HUD agent whose only action is one exact upstream native run."""

    def __init__(
        self,
        config: NativeOpenHandsConfig,
        *,
        executor: Callable[[NativeOpenHandsConfig, NativeTaskBinding], NativeReceipt] | None = None,
        worker_pool: Executor | None = None,
    ) -> None:
        self.config = config.normalized()
        self._executor = executor or execute_upstream_openhands
        self._worker_pool = worker_pool

    async def __call__(self, run: Any) -> None:
        await _run_and_record(run, self.config, self._executor, self._worker_pool)


class NativeOpenHandsBatchAgent(Agent):
    """Route each shared-Taskset call to its trace-private native config."""

    def __init__(
        self,
        configs: Mapping[str, NativeOpenHandsConfig],
        *,
        executor: Callable[[NativeOpenHandsConfig, NativeTaskBinding], NativeReceipt] | None = None,
        worker_pool: Executor | None = None,
        prelaunch_verifier: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._configs = {task_id: config.normalized() for task_id, config in configs.items()}
        self._executor = executor or execute_upstream_openhands
        self._worker_pool = worker_pool
        self._prelaunch_verifier = prelaunch_verifier

    async def __call__(self, run: Any) -> None:
        try:
            task_args = getattr(run, "_args", None)
            if not isinstance(task_args, Mapping):
                raise ValueError("HUD run does not expose the bound task setup arguments")
            binding = NativeTaskBinding.model_validate({"schema_version": "1", **task_args})
            config = self._configs[binding.task_id]
        except Exception as exc:
            # Use any profile only to encode a typed infrastructure error. The
            # taskset factory prevents this path for valid scheduled rows.
            config = next(iter(self._configs.values()))
            detail = f"{type(exc).__name__}: {exc}"

            def invalid_executor(
                _config: NativeOpenHandsConfig,
                invalid_binding: NativeTaskBinding,
            ) -> NativeReceipt:
                return NativeReceipt(
                    status="error",
                    task_id=invalid_binding.task_id,
                    server=invalid_binding.server,
                    run_profile=config.receipt_profile(),
                    error=f"native scheduler has no rollout config: {detail}",
                )

            await _run_and_record(run, config, invalid_executor, self._worker_pool)
            return
        if self._prelaunch_verifier is not None:
            trace_id = get_current_trace_id()
            if trace_id is None:
                raise RuntimeError("campaign prelaunch verification has no active HUD trace ID")
            await self._prelaunch_verifier(trace_id, binding.task_id)
        await _run_and_record(run, config, self._executor, self._worker_pool)


__all__ = [
    "NativeOpenHandsAgent",
    "NativeOpenHandsBatchAgent",
    "NativeOpenHandsConfig",
    "execute_upstream_openhands",
    "load_upstream_openhands",
]

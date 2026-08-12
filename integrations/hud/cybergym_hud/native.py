"""Exact upstream OpenHands 0.33 invocation wrapped as a HUD receipt agent."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

from hud.agents.base import Agent
from hud.types import Step

from .contract import repository_root, validate_contract
from .receipt import NativeReceipt, NativeRunProfile, NativeTaskBinding, normalize_server
from .upstream import require_upstream_agent_checkout

_UUID_INJECTION_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class NativeOpenHandsConfig:
    repository_root: Path
    data_dir: Path
    server: str
    model: str
    log_dir: Path
    tmp_dir: Path
    max_iter: int = 10
    timeout: int = 1200
    llm_api_key: str | None = None
    base_url: str = ""
    native_tool_calling: bool | None = None
    top_p: float = 1.0
    temperature: float = 0.0
    max_output_tokens: int = 2048
    seed: int | None = None
    silent: bool = False
    remove_tmp: bool = True
    debug: bool = False

    def normalized(self) -> NativeOpenHandsConfig:
        root = repository_root(self.repository_root)
        if self.max_iter < 1:
            raise ValueError("max_iter must be at least one")
        if self.timeout < 1:
            raise ValueError("timeout must be at least one second")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        return NativeOpenHandsConfig(
            repository_root=root,
            data_dir=self.data_dir.expanduser().resolve(),
            server=normalize_server(self.server),
            model=self.model,
            log_dir=self.log_dir.expanduser().resolve(),
            tmp_dir=self.tmp_dir.expanduser().resolve(),
            max_iter=self.max_iter,
            timeout=self.timeout,
            llm_api_key=self.llm_api_key,
            base_url=self.base_url,
            native_tool_calling=self.native_tool_calling,
            top_p=self.top_p,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            seed=self.seed,
            silent=self.silent,
            remove_tmp=self.remove_tmp,
            debug=self.debug,
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
            max_iter=self.max_iter,
            timeout_seconds=self.timeout,
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            seed=self.seed,
            native_tool_calling=self.native_tool_calling,
            base_url_mode="custom" if self.base_url else "provider-default",
        )


def load_upstream_openhands(root: Path) -> ModuleType:
    agents = require_upstream_agent_checkout(root)
    script = agents / "openhands/run.py"
    name = f"_cybergym_upstream_openhands_{abs(hash(script))}"
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load pinned upstream OpenHands runner at {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    fresh_uuid = uuid_factory()
    agent_id = fresh_uuid.hex
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
    # validation fails. Inject one real freshly generated UUID so the HUD
    # receipt always knows which upstream records belong to this attempt.
    with _UUID_INJECTION_LOCK:
        original_uuid4 = upstream.uuid4
        upstream.uuid4 = lambda: fresh_uuid
        try:
            returned = upstream.run_with_configs(openhands_args, task_args)
        except Exception as exc:
            return NativeReceipt(
                status="error",
                task_id=binding.task_id,
                server=binding.server,
                run_profile=run_profile,
                agent_id=agent_id,
                log_dir=str(receipt_log_dir),
                error=f"upstream run_with_configs failed: {type(exc).__name__}: {exc}",
            )
        finally:
            upstream.uuid4 = original_uuid4

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
    return NativeReceipt(
        status="completed",
        task_id=binding.task_id,
        server=binding.server,
        run_profile=run_profile,
        agent_id=agent_id,
        upstream_returned_agent_id=returned,
        log_dir=str(receipt_log_dir),
    )


class NativeOpenHandsAgent(Agent):
    """HUD agent whose only action is one exact upstream native run."""

    def __init__(
        self,
        config: NativeOpenHandsConfig,
        *,
        executor: Callable[[NativeOpenHandsConfig, NativeTaskBinding], NativeReceipt] | None = None,
    ) -> None:
        self.config = config.normalized()
        self._executor = executor or execute_upstream_openhands

    async def __call__(self, run: Any) -> None:
        try:
            binding = NativeTaskBinding.model_validate_json(run.prompt_text)
            if binding.server != self.config.server:
                raise ValueError("HUD task server does not match native runner configuration")
            receipt = await asyncio.to_thread(self._executor, self.config, binding)
        except Exception as exc:
            task_id = "arvo:invalid"
            server = self.config.server
            try:
                raw = json.loads(run.prompt_text)
                if isinstance(raw, dict):
                    task_id = str(raw.get("task_id", task_id))
                    server = str(raw.get("server", server))
                receipt = NativeReceipt(
                    status="error",
                    task_id=task_id,
                    server=server,
                    run_profile=self.config.receipt_profile(),
                    error=f"native scheduler failed: {type(exc).__name__}: {exc}",
                )
            except Exception:
                receipt = NativeReceipt(
                    status="error",
                    task_id="arvo:invalid",
                    server=self.config.server,
                    run_profile=self.config.receipt_profile(),
                    error=f"native scheduler failed: {type(exc).__name__}: {exc}",
                )

        payload = receipt.model_dump(mode="json")
        run.trace.content = receipt.model_dump_json()
        run.trace.extra.update(
            {
                "runner": receipt.runner,
                "native_openhands_receipt": payload,
            }
        )
        run.record(Step(source="agent", extra={"native_openhands_receipt": payload}))
        run.trace.stop_reason = "done"
        if receipt.status == "error":
            run.trace.status = "error"


__all__ = [
    "NativeOpenHandsAgent",
    "NativeOpenHandsConfig",
    "execute_upstream_openhands",
    "load_upstream_openhands",
]

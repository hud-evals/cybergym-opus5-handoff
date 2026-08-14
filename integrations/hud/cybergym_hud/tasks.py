"""Pure HUD receipt rows for upstream-native CyberGym runs."""

from __future__ import annotations

from hud import Task

from .contract import CONTRACT, openhands_system_prompt
from .receipt import NativeTaskBinding


def make_task(task_id: str, *, server: str, system_prompt: str | None = None) -> Task:
    binding = NativeTaskBinding(task_id=task_id, server=server)
    return Task(
        env="cybergym-og-native-receipt",
        id="run_upstream_openhands",
        args=binding.model_dump(exclude={"schema_version"}),
        slug=task_id.replace(":", "-"),
        agent_config={"system_prompt": system_prompt if system_prompt is not None else openhands_system_prompt()},
        columns={
            "benchmark": "cybergym-og",
            "difficulty": "level1",
            "upstream_commit": CONTRACT["benchmark"]["commit"],
            "agent_scaffold": "upstream-openhands-0.33-native",
            "primary_metric": "paper_era_agent_wide_any_of",
        },
    )


__all__ = ["make_task"]

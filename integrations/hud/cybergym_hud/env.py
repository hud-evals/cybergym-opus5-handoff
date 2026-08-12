"""Capability-free HUD environment that grades native OpenHands receipts."""

from __future__ import annotations

from hud.environment import Answer, Environment

from .grading import _error, grade_receipt
from .receipt import NativeReceipt, NativeTaskBinding

ENV_NAME = "cybergym-og-native-receipt"


def build_env() -> Environment:
    """Build a fresh receipt environment for one HUD runtime acquisition."""

    receipt_env = Environment(ENV_NAME, version="7656b71-native")

    @receipt_env.template(id="run_upstream_openhands", returns=NativeReceipt)
    async def run_upstream_openhands(task_id: str, server: str):
        binding = NativeTaskBinding(task_id=task_id, server=server)
        answer = yield binding.model_dump_json()
        if not isinstance(answer, Answer) or not isinstance(answer.content, NativeReceipt):
            yield _error("scheduler returned a malformed native OpenHands receipt")
            return
        yield await grade_receipt(binding, answer.content)

    return receipt_env


env = build_env()

__all__ = ["build_env", "env"]

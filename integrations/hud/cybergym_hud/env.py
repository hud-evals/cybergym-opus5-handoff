"""HUD receipt environment with observation-only native-workspace tracking."""

from __future__ import annotations

from pathlib import Path

from hud.environment import Answer, Environment, Workspace

from .cleanup import cleanup_tracked_root
from .contract import OG_PROMPT
from .grading import _error, grade_receipt
from .receipt import NativeReceipt, NativeTaskBinding

ENV_NAME = "cybergym-og-native-receipt"


def build_env(
    *,
    file_tracking_root: str | Path | None = None,
    cleanup_file_tracking_root: bool = False,
) -> Environment:
    """Build a fresh receipt environment for one HUD runtime acquisition.

    ``file_tracking_root`` is the upstream OpenHands ``tmp_dir``. The actual
    model-visible workspace is created beneath it by the unchanged upstream
    runner. Only HUD's observation-only ``filetracking/1`` capability is
    published; the workspace's SSH capability is deliberately withheld so the
    delegated model receives no new tool or filesystem access path.
    """

    receipt_env = Environment(ENV_NAME, version="7656b71-native")

    if file_tracking_root is not None:
        observed_workspace = Workspace(file_tracking_root, track_files=True)

        @receipt_env.initialize
        async def _start_file_tracking() -> None:
            await observed_workspace.start()
            receipt_env.add_capability(observed_workspace.file_tracking_capability())

        @receipt_env.shutdown
        async def _stop_file_tracking() -> None:
            await observed_workspace.stop()
            # HUD's rollout observer flushes before LocalRuntime shuts down the
            # environment. This restores upstream's ordinary remove_tmp policy
            # per completed slot instead of retaining all roots until the batch
            # finishes.
            if cleanup_file_tracking_root:
                cleanup_tracked_root(file_tracking_root)

    @receipt_env.template(id="run_upstream_openhands", returns=NativeReceipt)
    async def run_upstream_openhands(task_id: str, server: str):
        binding = NativeTaskBinding(task_id=task_id, server=server)
        # The HUD user turn is the exact prompt OpenHands receives. Binding
        # remains in Task.args and is not exposed as model-visible JSON.
        answer = yield OG_PROMPT
        if not isinstance(answer, Answer) or not isinstance(answer.content, NativeReceipt):
            yield _error("scheduler returned a malformed native OpenHands receipt")
            return
        yield await grade_receipt(binding, answer.content)

    return receipt_env


env = build_env()

__all__ = ["build_env", "env"]

"""Pinned OpenHands runtime client for an integration-owned Daytona sandbox.

The CyberGym parent process creates, journals, tunnels, isolates, and deletes
the sandbox.  This child-side class only attaches OpenHands' unchanged action
client to the loopback URL supplied by that parent.  No Daytona credential is
available in the OpenHands controller process or model workspace.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import tenacity
from openhands.core.config import AppConfig
from openhands.events.stream import EventStream
from openhands.runtime.impl.action_execution.action_execution_client import (
    ActionExecutionClient,
)
from openhands.runtime.plugins.requirement import PluginRequirement
from openhands.utils.async_utils import call_sync_from_async
from openhands.utils.tenacity_stop import stop_if_should_exit


class CyberGymDaytonaAttachedRuntime(ActionExecutionClient):
    """Attach to the private SSH-forwarded action server prepared by HUD."""

    def __init__(
        self,
        config: AppConfig,
        event_stream: EventStream,
        sid: str = "default",
        plugins: list[PluginRequirement] | None = None,
        env_vars: dict[str, str] | None = None,
        status_callback: Callable | None = None,
        attach_to_existing: bool = False,
        headless_mode: bool = True,
        **kwargs: object,
    ) -> None:
        action_url = os.environ.get("CYBERGYM_DAYTONA_ACTION_URL", "").strip()
        if not action_url.startswith("http://127.0.0.1:"):
            raise RuntimeError("CyberGym Daytona action URL is missing or not loopback-only")
        self.api_url = action_url.rstrip("/")
        config.workspace_mount_path_in_sandbox = "/workspace"
        super().__init__(
            config,
            event_stream,
            sid,
            plugins,
            env_vars,
            status_callback,
            attach_to_existing,
            headless_mode,
            **kwargs,
        )

    def _get_action_execution_server_host(self) -> str:
        return self.api_url

    @tenacity.retry(
        stop=tenacity.stop_after_delay(120) | stop_if_should_exit(),
        wait=tenacity.wait_fixed(1),
        reraise=True,
    )
    def _wait_until_alive(self) -> None:
        self.check_if_alive()

    async def connect(self) -> None:
        self.send_status_message("STATUS$WAITING_FOR_CLIENT")
        await call_sync_from_async(self._wait_until_alive)
        if not self.attach_to_existing:
            await call_sync_from_async(self.setup_initial_env)
        self._runtime_initialized = True
        self.send_status_message(" ")

    def close(self) -> None:
        # The parent process owns the SSH tunnel and exact sandbox deletion.
        super().close()


__all__ = ["CyberGymDaytonaAttachedRuntime"]

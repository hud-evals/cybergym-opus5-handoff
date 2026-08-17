"""Integration-owned Daytona placement for the separate CyberGym lane."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncssh
import httpx
import tomli_w
from daytona import (
    CreateSandboxFromImageParams,
    Daytona,
    DaytonaNotFoundError,
    Image,
    Resources,
    SessionExecuteRequest,
)

DAYTONA_IMAGE = "ghcr.io/all-hands-ai/runtime@sha256:ff8d9ef50ceb475130de5bca59d5c8f4dc9c45e11566ebaa6cae6a95b388d989"
ACTION_PORT = 4444
GRADER_TUNNEL_PORT = 8666
SSH_HOST = "ssh.app.daytona.io"
RUNTIME_CLASS = "cybergym_daytona_attached_runtime.CyberGymDaytonaAttachedRuntime"
LEDGER_SCHEMA = "cybergym.daytona-sandbox-ledger.v1"
DAYTONA_CONTRACT = Path(__file__).with_name("daytona-fidelity-contract.json")


def validate_daytona_contract() -> dict[str, Any]:
    payload = json.loads(DAYTONA_CONTRACT.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != "1"
        or payload.get("canonical_native_result") is not False
        or payload.get("merge_with_native_campaign") is not False
        or payload.get("job_name") != "cybergym-gpt5.6-sol-2"
        or payload.get("runtime", {}).get("image") != DAYTONA_IMAGE
        or payload.get("runtime", {}).get("network") != "block_all_after_action_server_and_ssh_tunnel"
    ):
        raise RuntimeError("CyberGym Daytona fidelity contract drifted")
    return payload


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short Daytona ledger write")
        view = view[written:]


def record_sandbox_event(
    path: Path,
    *,
    event: str,
    sandbox_id: str,
    task_id: str,
) -> None:
    if event not in {"created", "deleted"}:
        raise ValueError("unsupported Daytona ledger event")
    payload = {
        "schema": LEDGER_SCHEMA,
        "event": event,
        "sandbox_id": sandbox_id,
        "task_id": task_id,
        "recorded_at": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, (json.dumps(payload, sort_keys=True) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class _SshTunnel:
    def __init__(
        self,
        *,
        username: str,
        known_hosts: Path,
        grader_host: str,
        grader_port: int,
    ) -> None:
        self.username = username
        self.known_hosts = known_hosts
        self.grader_host = grader_host
        self.grader_port = grader_port
        self.action_local_port: int | None = None
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._error: BaseException | None = None

    def start(self, *, timeout: float = 30.0) -> None:
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("Daytona SSH tunnel did not become ready")
        if self._error is not None:
            raise RuntimeError("Daytona SSH tunnel failed") from self._error
        if self.action_local_port is None:
            raise RuntimeError("Daytona SSH tunnel omitted its local action port")

    def close(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=30)
        if self._thread.is_alive():
            raise RuntimeError("Daytona SSH tunnel did not stop")
        if self._error is not None:
            raise RuntimeError("Daytona SSH tunnel failed") from self._error

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:
            self._error = exc
            self._ready.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with asyncssh.connect(
            SSH_HOST,
            username=self.username,
            known_hosts=str(self.known_hosts),
            keepalive_interval=15,
            keepalive_count_max=4,
        ) as connection:
            local_listener = await connection.forward_local_port(
                "127.0.0.1",
                0,
                "127.0.0.1",
                ACTION_PORT,
            )
            remote_listener = await connection.forward_remote_port(
                "127.0.0.1",
                GRADER_TUNNEL_PORT,
                self.grader_host,
                self.grader_port,
            )
            try:
                self.action_local_port = local_listener.get_port()
                self._ready.set()
                await self._stop.wait()
            finally:
                remote_listener.close()
                local_listener.close()
                await remote_listener.wait_closed()
                await local_listener.wait_closed()


@dataclass(frozen=True)
class DaytonaPreparedRuntime:
    action_url: str
    sandbox_id: str


def _server_address(server: str) -> tuple[str, int]:
    from urllib.parse import urlsplit

    parsed = urlsplit(server)
    if parsed.scheme != "http" or parsed.hostname is None:
        raise ValueError("Daytona grader upstream must be a private HTTP URL")
    return parsed.hostname, parsed.port or 80


def _action_server_command() -> str:
    return (
        "cd /openhands/code && "
        "/openhands/micromamba/bin/micromamba run -n openhands poetry run "
        "python -u -m openhands.runtime.action_execution_server "
        f"{ACTION_PORT} --working-dir /workspace "
        "--plugins agent_skills jupyter --username root --user-id 0"
    )


def _wait_action_server(url: str, *, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{url}/alive", timeout=5.0)
            response.raise_for_status()
            return
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            time.sleep(1)
    raise TimeoutError("Daytona OpenHands action server did not become ready") from last_error


def _require_blocked_network(sandbox: Any) -> None:
    dns = sandbox.process.exec(
        'python3 -c "import socket,sys; '
        "\ntry: socket.getaddrinfo('github.com',443)"
        "\nexcept OSError: sys.exit(0)"
        '\nsys.exit(1)"',
        timeout=15,
    )
    ip = sandbox.process.exec(
        'python3 -c "import socket,sys; s=socket.socket(); s.settimeout(3); '
        "\ntry: s.connect(('1.1.1.1',443))"
        "\nexcept OSError: sys.exit(0)"
        '\nsys.exit(1)"',
        timeout=15,
    )
    grader = sandbox.process.exec(
        f"python3 -c \"import socket; socket.create_connection(('127.0.0.1',{GRADER_TUNNEL_PORT}),3).close()\"",
        timeout=15,
    )
    if dns.exit_code != 0 or ip.exit_code != 0 or grader.exit_code != 0:
        raise RuntimeError("Daytona network isolation or private grader tunnel proof failed")


def _delete_exact(daytona: Daytona, sandbox: Any) -> None:
    sandbox_id = str(sandbox.id)
    for attempt in range(3):
        try:
            daytona.delete(sandbox)
            break
        except DaytonaNotFoundError:
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    deadline = time.monotonic() + 60
    while True:
        try:
            daytona.get(sandbox_id)
        except DaytonaNotFoundError:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Daytona sandbox remained after deletion: {sandbox_id}")
        time.sleep(1)


@contextmanager
def prepared_daytona_runtime(
    *,
    task_id: str,
    server: str,
    ledger_path: Path,
    known_hosts: Path,
) -> Iterator[DaytonaPreparedRuntime]:
    """Create and prove one private Daytona OpenHands runtime, then delete it."""

    validate_daytona_contract()
    api_key = os.environ.get("DAYTONA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DAYTONA_API_KEY is required for the separate Daytona lane")
    if not known_hosts.is_file() or stat.S_ISLNK(known_hosts.lstat().st_mode):
        raise RuntimeError("Daytona SSH known-hosts pin is missing or unsafe")
    grader_host, grader_port = _server_address(server)
    daytona = Daytona()
    sandbox = daytona.create(
        CreateSandboxFromImageParams(
            name=f"cybergym-{uuid4().hex}",
            image=Image.base(DAYTONA_IMAGE),
            resources=Resources(cpu=4, memory=8, disk=10),
            ephemeral=True,
            auto_stop_interval=0,
            os_user="root",
            public=False,
            labels={"ai.hud.cybergym.lane": "daytona-no-internet-v1"},
        ),
        timeout=180,
    )
    sandbox_id = str(sandbox.id)
    record_sandbox_event(
        ledger_path,
        event="created",
        sandbox_id=sandbox_id,
        task_id=task_id,
    )
    tunnel: _SshTunnel | None = None
    try:
        sandbox.process.create_session("openhands-action-server")
        sandbox.process.execute_session_command(
            "openhands-action-server",
            SessionExecuteRequest(command=_action_server_command(), run_async=True),
        )
        ssh = sandbox.create_ssh_access(expires_in_minutes=120)
        tunnel = _SshTunnel(
            username=ssh.token,
            known_hosts=known_hosts,
            grader_host=grader_host,
            grader_port=grader_port,
        )
        tunnel.start()
        if tunnel.action_local_port is None:
            raise RuntimeError("Daytona SSH tunnel omitted its action port")
        action_url = f"http://127.0.0.1:{tunnel.action_local_port}"
        _wait_action_server(action_url)
        sandbox.update_network_settings(network_block_all=True)
        _require_blocked_network(sandbox)
        _wait_action_server(action_url)
        yield DaytonaPreparedRuntime(action_url=action_url, sandbox_id=sandbox_id)
    finally:
        if tunnel is not None:
            tunnel.close()
        _delete_exact(daytona, sandbox)
        record_sandbox_event(
            ledger_path,
            event="deleted",
            sandbox_id=sandbox_id,
            task_id=task_id,
        )


def configure_attached_runtime(config_path: Path) -> Path:
    """Set the pinned OpenHands child to the integration-owned attached runtime."""

    import tomllib

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    core = config.get("core")
    if not isinstance(core, dict):
        raise RuntimeError("OpenHands config omitted its core section")
    core["runtime"] = RUNTIME_CLASS
    core["workspace_mount_path_in_sandbox"] = "/workspace"
    encoded = tomli_w.dumps(config).encode()
    temporary = config_path.with_name(f".{config_path.name}.daytona.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, config_path)
    return Path(str(core["workspace_base"]))


def rewrite_submit_server(workspace: Path, *, source: str) -> None:
    submit = workspace / "submit.sh"
    before = submit.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError("generated submit.sh is not a regular file")
    text = submit.read_text(encoding="utf-8")
    if text.count(source) != 1:
        raise RuntimeError("generated submit.sh did not contain exactly one private server URL")
    submit.write_text(
        text.replace(source, f"http://127.0.0.1:{GRADER_TUNNEL_PORT}"),
        encoding="utf-8",
    )


__all__ = [
    "DAYTONA_IMAGE",
    "DaytonaPreparedRuntime",
    "configure_attached_runtime",
    "prepared_daytona_runtime",
    "record_sandbox_event",
    "rewrite_submit_server",
    "validate_daytona_contract",
]

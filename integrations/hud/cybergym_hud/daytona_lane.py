"""Integration-owned Daytona placement for the separate CyberGym lane."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator
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
    FileUpload,
    Image,
    Resources,
    SessionExecuteRequest,
)

from .artifact_storage import (
    enforce_private_file_mode,
    has_private_storage,
    is_trusted_artifact_volume_path,
)

DAYTONA_IMAGE = "ghcr.io/all-hands-ai/runtime@sha256:ff8d9ef50ceb475130de5bca59d5c8f4dc9c45e11566ebaa6cae6a95b388d989"
ACTION_PORT = 4444
GRADER_TUNNEL_PORT = 8666
SSH_HOST = "ssh.app.daytona.io"
RUNTIME_CLASS = "cybergym_daytona_attached_runtime.CyberGymDaytonaAttachedRuntime"
LEDGER_SCHEMA = "cybergym.daytona-sandbox-ledger.v1"
DAYTONA_CONTRACT = Path(__file__).with_name("daytona-fidelity-contract.json")
VISIBLE_WORKSPACE_FILES = frozenset({"README.md", "description.txt", "repo-vul.tar.gz", "submit.sh"})
MAX_WORKSPACE_BYTES = 4 * 1024 * 1024 * 1024
MAX_LEDGER_BYTES = 8 * 1024 * 1024
_VOLUME_LEDGER_LOCKS: dict[str, threading.Lock] = {}
_VOLUME_LEDGER_LOCKS_GUARD = threading.Lock()


def validate_daytona_contract() -> dict[str, Any]:
    payload = json.loads(DAYTONA_CONTRACT.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != "1"
        or payload.get("canonical_native_result") is not False
        or payload.get("merge_with_native_campaign") is not False
        or payload.get("job_name") != "cybergym-opus5-cyber"
        or payload.get("agent", {}).get("model") != "claude-opus-5"
        or payload.get("agent", {}).get("reasoning_effort") is not None
        or payload.get("agent", {}).get("adaptive_effort") != "low"
        or payload.get("agent", {}).get("max_output_tokens") != 16000
        or payload.get("runtime", {}).get("image") != DAYTONA_IMAGE
        or payload.get("runtime", {}).get("max_concurrent") != 60
        or payload.get("runtime", {}).get("network")
        != "host_cidr_allowlist_plus_tls_resolve_before_workspace_stage_and_action_server"
        or payload.get("accounting", {}).get("retry_policy")
        != "terminal_error_rows_remain_pending_for_exact_automatic_retry"
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
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
    if is_trusted_artifact_volume_path(path):
        key = str(path.resolve(strict=False))
        with _VOLUME_LEDGER_LOCKS_GUARD:
            lock = _VOLUME_LEDGER_LOCKS.setdefault(key, threading.Lock())
        with lock:
            existing = path.read_bytes() if path.exists() else b""
            if len(existing) + len(encoded) > MAX_LEDGER_BYTES:
                raise RuntimeError("Daytona sandbox ledger exceeds its byte limit")
            path.write_bytes(existing + encoded)
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        enforce_private_file_mode(descriptor, path)
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def open_sandbox_bindings(path: Path) -> dict[str, str]:
    """Validate the append-only ledger and return created-but-not-deleted IDs."""

    if not path.exists():
        return {}
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or not has_private_storage(path, before.st_mode)
        or before.st_size > MAX_LEDGER_BYTES
    ):
        raise RuntimeError("Daytona sandbox ledger is not a private bounded regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
            raise RuntimeError("Daytona sandbox ledger changed while opening")
        chunks: list[bytes] = []
        total = 0
        while block := os.read(descriptor, min(1024 * 1024, MAX_LEDGER_BYTES + 1 - total)):
            chunks.append(block)
            total += len(block)
            if total > MAX_LEDGER_BYTES:
                raise RuntimeError("Daytona sandbox ledger exceeds its byte limit")
        encoded = b"".join(chunks)
    finally:
        os.close(descriptor)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RuntimeError("Daytona sandbox ledger changed while reading")
    open_bindings: dict[str, str] = {}
    closed: set[str] = set()
    for line in encoded.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Daytona sandbox ledger contains malformed JSON") from exc
        if not isinstance(row, dict) or set(row) != {
            "schema",
            "event",
            "sandbox_id",
            "task_id",
            "recorded_at",
        }:
            raise RuntimeError("Daytona sandbox ledger row shape drifted")
        event = row.get("event")
        sandbox_id = row.get("sandbox_id")
        task_id = row.get("task_id")
        if (
            row.get("schema") != LEDGER_SCHEMA
            or event not in {"created", "deleted"}
            or not isinstance(sandbox_id, str)
            or not sandbox_id
            or not isinstance(task_id, str)
            or not task_id
            or not isinstance(row.get("recorded_at"), int | float)
        ):
            raise RuntimeError("Daytona sandbox ledger row is invalid")
        if event == "created":
            if sandbox_id in open_bindings or sandbox_id in closed:
                raise RuntimeError("Daytona sandbox ledger repeats a creation")
            open_bindings[sandbox_id] = task_id
        else:
            if open_bindings.pop(sandbox_id, None) != task_id:
                raise RuntimeError("Daytona sandbox ledger deletion is unbound")
            closed.add(sandbox_id)
    return open_bindings


def reconcile_daytona_sandboxes(
    path: Path,
    *,
    expected_task_ids: set[str],
    daytona: Daytona | None = None,
) -> tuple[str, ...]:
    """Delete exact ledger-owned leftovers before any new paid rollout."""

    open_bindings = open_sandbox_bindings(path)
    unexpected = set(open_bindings.values()) - expected_task_ids
    if unexpected:
        raise RuntimeError(f"Daytona sandbox ledger contains tasks outside this campaign: {sorted(unexpected)}")
    if not open_bindings:
        return ()
    client = daytona or Daytona()
    reconciled: list[str] = []
    for sandbox_id, task_id in sorted(open_bindings.items()):
        try:
            sandbox = client.get(sandbox_id)
        except DaytonaNotFoundError:
            sandbox = None
        if sandbox is not None:
            if (
                sandbox.labels.get("ai.hud.cybergym.lane") != "daytona-no-internet-v1"
                or not sandbox.name.startswith("cybergym-")
                or sandbox.public is not False
            ):
                raise RuntimeError("refusing to delete a Daytona sandbox without the exact lane identity")
            _delete_exact(client, sandbox)
        record_sandbox_event(
            path,
            event="deleted",
            sandbox_id=sandbox_id,
            task_id=task_id,
        )
        reconciled.append(sandbox_id)
    return tuple(reconciled)


class _SshTunnel:
    def __init__(
        self,
        *,
        username: str,
        known_hosts: Path,
    ) -> None:
        self.username = username
        self.known_hosts = known_hosts
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
        if self._thread.is_alive() and self._loop is not None and not self._loop.is_closed() and self._stop is not None:
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
            try:
                self.action_local_port = local_listener.get_port()
                self._ready.set()
                await self._stop.wait()
            finally:
                local_listener.close()
                await local_listener.wait_closed()


@dataclass(frozen=True)
class DaytonaPreparedRuntime:
    action_url: str
    submission_url: str
    submission_curl_resolve: str
    sandbox_id: str


def _relay_settings() -> tuple[str, str, int, str, str, Path | None, str | None]:
    from urllib.parse import urlsplit

    base_url = os.environ.get("CG_DAYTONA_RELAY_URL", "").strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or parsed.hostname is None or parsed.path:
        raise ValueError("CG_DAYTONA_RELAY_URL must be an HTTPS origin without a path")
    port = parsed.port or 443
    if port not in {443, 8443, 10000}:
        raise ValueError("CG_DAYTONA_RELAY_URL must use a supported Tailscale Funnel HTTPS port")
    admin_token = os.environ.get("CG_DAYTONA_RELAY_ADMIN_TOKEN", "").strip() or None
    if admin_token is not None and (
        len(admin_token) != 64 or any(character not in "0123456789abcdef" for character in admin_token)
    ):
        raise ValueError("CG_DAYTONA_RELAY_ADMIN_TOKEN must be 32-byte lowercase hexadecimal")
    registry_raw = os.environ.get("CG_DAYTONA_RELAY_REGISTRY", "").strip()
    if not registry_raw and admin_token is None:
        raise ValueError("CG_DAYTONA_RELAY_REGISTRY or remote administrator authentication is required")
    cidrs_raw = os.environ.get("CG_DAYTONA_RELAY_CIDRS", "").strip()
    cidrs = [value.strip() for value in cidrs_raw.split(",") if value.strip()]
    if not cidrs:
        raise ValueError("CG_DAYTONA_RELAY_CIDRS is required")
    relay_ipv4: str | None = None
    for value in cidrs:
        network = ipaddress.ip_network(value, strict=True)
        if network.is_private or network.is_loopback or network.num_addresses != 1:
            raise ValueError("relay allowlist must contain only public /32 or /128 hosts")
        if network.version == 4 and relay_ipv4 is None:
            relay_ipv4 = str(network.network_address)
    if relay_ipv4 is None:
        raise ValueError("relay allowlist requires at least one public IPv4 host")
    return (
        base_url,
        parsed.hostname,
        port,
        ",".join(cidrs),
        relay_ipv4,
        Path(registry_raw).expanduser().resolve() if registry_raw else None,
        admin_token,
    )


def _register_relay_local(registry: Path, *, task_id: str) -> tuple[str, Callable[[], None]]:
    registry.mkdir(parents=True, exist_ok=True)
    os.chmod(registry, 0o700)
    token = secrets.token_hex(32)
    path = registry / f"{token}.json"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(
            descriptor,
            json.dumps(
                {"task_id": task_id, "expires_at": int(time.time()) + 2 * 60 * 60},
                sort_keys=True,
            ).encode(),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return token, lambda: path.unlink(missing_ok=True)


def _register_relay_remote(
    base_url: str,
    *,
    admin_token: str,
    task_id: str,
) -> tuple[str, Callable[[], None]]:
    headers = {"Authorization": f"Bearer {admin_token}"}
    try:
        response = httpx.post(
            f"{base_url}/admin/v1/bindings",
            json={"task_id": task_id},
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise RuntimeError("remote Daytona relay registration failed") from exc
    token = payload.get("token") if isinstance(payload, dict) else None
    if (
        not isinstance(token, str)
        or len(token) != 64
        or any(character not in "0123456789abcdef" for character in token)
    ):
        raise RuntimeError("remote Daytona relay returned an invalid binding")

    def release() -> None:
        try:
            deleted = httpx.delete(
                f"{base_url}/admin/v1/bindings/{token}",
                headers=headers,
                timeout=30,
            )
            deleted.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError("remote Daytona relay binding cleanup failed") from exc

    return token, release


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


def _require_blocked_network(
    sandbox: Any,
    *,
    relay_base_url: str,
    relay_hostname: str,
    relay_port: int,
    relay_ipv4: str,
) -> None:
    deadline = time.monotonic() + 60
    observed = (-1, -1, -1)
    while time.monotonic() < deadline:
        web = sandbox.process.exec(
            "curl -fsS --max-time 5 https://github.com >/dev/null 2>&1; test $? -ne 0",
            timeout=15,
        )
        ip = sandbox.process.exec(
            'python3 -c "import socket,sys; s=socket.socket(); s.settimeout(3); '
            "\ntry: s.connect(('1.1.1.1',443))"
            "\nexcept OSError: sys.exit(0)"
            '\nsys.exit(1)"',
            timeout=15,
        )
        relay = sandbox.process.exec(
            f"curl -fsS --max-time 10 --resolve {relay_hostname}:{relay_port}:{relay_ipv4} "
            f"{relay_base_url}/healthz >/dev/null",
            timeout=15,
        )
        observed = (web.exit_code, ip.exit_code, relay.exit_code)
        if observed == (0, 0, 0):
            return
        time.sleep(2)
    raise RuntimeError(
        "Daytona network isolation or task relay proof failed: "
        f"web_block={observed[0]}, raw_ip_block={observed[1]}, relay={observed[2]}"
    )


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


def _hash_regular_file(path: Path) -> tuple[int, int, str]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"Daytona workspace source is not a regular file: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RuntimeError(f"Daytona workspace source changed while hashing: {path.name}")
    return stat.S_IMODE(before.st_mode), before.st_size, digest.hexdigest()


def stage_workspace(sandbox: Any, workspace: Path) -> None:
    """Upload and verify the exact visible CyberGym task workspace."""

    root = workspace.lstat()
    if not stat.S_ISDIR(root.st_mode):
        raise RuntimeError("Daytona workspace source is not a directory")
    entries = {path.name: path for path in workspace.iterdir()}
    if set(entries) != VISIBLE_WORKSPACE_FILES:
        raise RuntimeError(f"Daytona workspace files drifted: {sorted(entries)}")
    receipts = {name: _hash_regular_file(entries[name]) for name in sorted(entries)}
    if sum(size for _mode, size, _digest in receipts.values()) > MAX_WORKSPACE_BYTES:
        raise RuntimeError("Daytona workspace exceeds the staged byte limit")

    reset = sandbox.process.exec("rm -rf -- /workspace && install -d -m 0755 /workspace", timeout=60)
    if reset.exit_code != 0:
        raise RuntimeError("could not reset the ephemeral Daytona workspace")
    sandbox.fs.upload_files(
        [FileUpload(str(entries[name]), f"/workspace/{name}") for name in sorted(VISIBLE_WORKSPACE_FILES)],
        timeout=3600,
    )
    for name, (mode, _size, _digest) in receipts.items():
        sandbox.fs.set_file_permissions(
            f"/workspace/{name}",
            mode=f"{mode:04o}",
            owner="root",
            group="root",
        )
    command = "cd /workspace && sha256sum -- " + " ".join(sorted(VISIBLE_WORKSPACE_FILES))
    verified = sandbox.process.exec(command, timeout=3600)
    if verified.exit_code != 0:
        raise RuntimeError("could not verify the staged Daytona workspace")
    observed: dict[str, str] = {}
    for line in verified.result.splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or name in observed:
            raise RuntimeError("Daytona workspace digest output was malformed")
        observed[name] = digest
    expected = {name: digest for name, (_mode, _size, digest) in receipts.items()}
    if observed != expected:
        raise RuntimeError("Daytona workspace upload did not preserve exact bytes")


@contextmanager
def prepared_daytona_runtime(
    *,
    task_id: str,
    server: str,
    ledger_path: Path,
    known_hosts: Path,
    workspace: Path,
) -> Iterator[DaytonaPreparedRuntime]:
    """Create and prove one private Daytona OpenHands runtime, then delete it."""

    validate_daytona_contract()
    action_transport = os.environ.get("CG_DAYTONA_ACTION_TRANSPORT", "ssh-local").strip()
    if action_transport not in {"ssh-local", "signed-preview"}:
        raise RuntimeError("CG_DAYTONA_ACTION_TRANSPORT must be ssh-local or signed-preview")
    api_key = os.environ.get("DAYTONA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DAYTONA_API_KEY is required for the separate Daytona lane")
    if action_transport == "ssh-local" and (not known_hosts.is_file() or stat.S_ISLNK(known_hosts.lstat().st_mode)):
        raise RuntimeError("Daytona SSH known-hosts pin is missing or unsafe")
    if not server.startswith("http://"):
        raise ValueError("Daytona lane requires the private HTTP grader identity")
    (
        relay_base_url,
        relay_hostname,
        relay_port,
        relay_cidrs,
        relay_ipv4,
        relay_registry,
        relay_admin_token,
    ) = _relay_settings()
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
    signed_preview: Any | None = None
    release_relay: Callable[[], None] | None = None
    try:
        if relay_admin_token is None:
            if relay_registry is None:
                raise RuntimeError("local Daytona relay registry is missing")
            relay_token, release_relay = _register_relay_local(relay_registry, task_id=task_id)
        else:
            relay_token, release_relay = _register_relay_remote(
                relay_base_url,
                admin_token=relay_admin_token,
                task_id=task_id,
            )
        sandbox.update_network_settings(
            network_allow_list=relay_cidrs,
        )
        _require_blocked_network(
            sandbox,
            relay_base_url=relay_base_url,
            relay_hostname=relay_hostname,
            relay_port=relay_port,
            relay_ipv4=relay_ipv4,
        )
        submission_url = f"{relay_base_url}/{relay_token}"
        submission_curl_resolve = f"{relay_hostname}:{relay_port}:{relay_ipv4}"
        rewrite_submit_server(
            workspace,
            source=server,
            replacement=submission_url,
            curl_resolve=submission_curl_resolve,
        )
        stage_workspace(sandbox, workspace)
        sandbox.process.create_session("openhands-action-server")
        sandbox.process.execute_session_command(
            "openhands-action-server",
            SessionExecuteRequest(command=_action_server_command(), run_async=True),
        )
        if action_transport == "ssh-local":
            ssh = sandbox.create_ssh_access(expires_in_minutes=120)
            tunnel = _SshTunnel(
                username=ssh.token,
                known_hosts=known_hosts,
            )
            tunnel.start()
            if tunnel.action_local_port is None:
                raise RuntimeError("Daytona SSH tunnel omitted its action port")
            action_url = f"http://127.0.0.1:{tunnel.action_local_port}"
        else:
            signed_preview = sandbox.create_signed_preview_url(
                ACTION_PORT,
                expires_in_seconds=2 * 60 * 60,
            )
            action_url = str(signed_preview.url)
        _wait_action_server(action_url)
        yield DaytonaPreparedRuntime(
            action_url=action_url,
            submission_url=submission_url,
            submission_curl_resolve=submission_curl_resolve,
            sandbox_id=sandbox_id,
        )
    finally:
        tunnel_error: BaseException | None = None
        if tunnel is not None:
            try:
                tunnel.close()
            except BaseException as exc:
                tunnel_error = exc
        if signed_preview is not None:
            try:
                sandbox.expire_signed_preview_url(ACTION_PORT, str(signed_preview.token))
            except BaseException as exc:
                tunnel_error = exc
        try:
            _delete_exact(daytona, sandbox)
        finally:
            record_sandbox_event(
                ledger_path,
                event="deleted",
                sandbox_id=sandbox_id,
                task_id=task_id,
            )
        if release_relay is not None:
            release_relay()
        if tunnel_error is not None:
            raise RuntimeError("Daytona action transport cleanup failed") from tunnel_error


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


def rewrite_submit_server(
    workspace: Path,
    *,
    source: str,
    replacement: str,
    curl_resolve: str,
) -> None:
    submit = workspace / "submit.sh"
    before = submit.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError("generated submit.sh is not a regular file")
    text = submit.read_text(encoding="utf-8")
    if text.count(source) != 1:
        raise RuntimeError("generated submit.sh did not contain exactly one private server URL")
    if text.count("curl -X POST") != 1:
        raise RuntimeError("generated submit.sh did not contain the expected curl invocation")
    submit.write_text(
        text.replace(source, replacement).replace(
            "curl -X POST",
            f"curl --resolve {curl_resolve} -X POST",
        ),
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

"""Fail-closed attestation for the live binary-only CyberGym grader server.

This module deliberately reads only non-secret systemd properties and procfs.
It never reads a unit's environment or an EnvironmentFile.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import ipaddress
import json
import os
import pwd
import re
import shlex
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SYSTEMCTL = Path("/usr/bin/systemctl")
GIT = Path("/usr/bin/git")
DEFAULT_UNIT = "cybergym-server.service"
DEFAULT_HELPER = Path("integrations/hud/ops/server.sh")
DEPLOYMENT_SEAL_SCHEMA = "1"
BINARY_MANIFEST_SCHEMA = "1"
TREE_DIGEST_ALGORITHM = "cybergym_hud.catalog_preflight._tree_digest/v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")
_EXEC_PATH_RE = re.compile(r"(?:^|[ {;])path=([^ ;}]+)")


class GraderAttestationError(RuntimeError):
    """The live grader could not be bound to its expected deployment."""


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    ppid: int
    uid: int
    gid: int
    start_ticks: int
    cwd: Path
    executable: Path
    argv: tuple[str, ...]


@dataclass(frozen=True)
class ListenerSnapshot:
    pid: int
    address: str
    port: int
    inode: int


@dataclass(frozen=True)
class ServiceSnapshot:
    unit: str
    active_state: str
    sub_state: str
    invocation_id: str
    main_pid: int
    control_group: str
    exec_start: str
    fragment_paths: tuple[Path, ...]
    user: str
    group: str
    processes: tuple[ProcessSnapshot, ...]


def _run_bytes(argv: list[str]) -> bytes:
    result = subprocess.run(  # noqa: S603 - argv contains only fixed binaries and explicit arguments
        argv,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise GraderAttestationError(f"attestation command failed: {Path(argv[0]).name}")
    return result.stdout


def _run_text(argv: list[str]) -> str:
    try:
        return _run_bytes(argv).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GraderAttestationError(f"attestation command returned non-UTF-8: {Path(argv[0]).name}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except OSError as exc:
        raise GraderAttestationError(f"cannot hash attestation file: {path}") from exc
    return digest.hexdigest()


def _systemd_properties(unit: str) -> dict[str, str]:
    names = (
        "ActiveState",
        "SubState",
        "InvocationID",
        "MainPID",
        "ControlGroup",
        "ExecStart",
        "FragmentPath",
        "DropInPaths",
        "User",
        "Group",
    )
    argv = [str(SYSTEMCTL), "show", unit, "--no-pager"]
    argv.extend(f"--property={name}" for name in names)
    properties: dict[str, str] = {}
    for line in _run_text(argv).splitlines():
        key, separator, value = line.partition("=")
        if separator and key in names:
            properties[key] = value
    if set(properties) != set(names):
        raise GraderAttestationError("systemd did not return every required non-secret property")
    return properties


def _parse_proc_stat(path: Path) -> tuple[int, int]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GraderAttestationError(f"cannot read process identity: {path}") from exc
    closing = raw.rfind(")")
    fields = raw[closing + 2 :].split() if closing >= 0 else []
    if len(fields) <= 19:
        raise GraderAttestationError(f"malformed process identity: {path}")
    try:
        return int(fields[1]), int(fields[19])
    except ValueError as exc:
        raise GraderAttestationError(f"malformed process identity: {path}") from exc


def _parse_proc_ids(path: Path) -> tuple[int, int]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GraderAttestationError(f"cannot read process credentials: {path}") from exc
    values: dict[str, int] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if key in {"Uid", "Gid"} and separator:
            try:
                values[key] = int(value.split()[0])
            except (IndexError, ValueError) as exc:
                raise GraderAttestationError(f"malformed process credentials: {path}") from exc
    if set(values) != {"Uid", "Gid"}:
        raise GraderAttestationError(f"missing process credentials: {path}")
    return values["Uid"], values["Gid"]


def _read_process(pid: int, *, proc_root: Path) -> ProcessSnapshot:
    root = proc_root / str(pid)
    ppid, start_ticks = _parse_proc_stat(root / "stat")
    uid, gid = _parse_proc_ids(root / "status")
    try:
        argv_bytes = (root / "cmdline").read_bytes()
        argv = tuple(part.decode("utf-8", errors="strict") for part in argv_bytes.split(b"\0") if part)
        cwd = Path(os.readlink(root / "cwd"))
        executable = Path(os.readlink(root / "exe"))
    except (OSError, UnicodeDecodeError) as exc:
        raise GraderAttestationError(f"cannot inspect process {pid}") from exc
    if not argv:
        raise GraderAttestationError(f"process {pid} has an empty command line")
    return ProcessSnapshot(
        pid=pid,
        ppid=ppid,
        uid=uid,
        gid=gid,
        start_ticks=start_ticks,
        cwd=cwd,
        executable=executable,
        argv=argv,
    )


def _safe_cgroup_path(control_group: str, *, cgroup_root: Path) -> Path:
    relative = Path(control_group.lstrip("/"))
    if not control_group.startswith("/") or ".." in relative.parts:
        raise GraderAttestationError("systemd returned an unsafe control-group path")
    return cgroup_root / relative


def capture_service_snapshot(
    unit: str = DEFAULT_UNIT,
    *,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> ServiceSnapshot:
    """Capture a stable systemd/cgroup/procfs view without reading environment data."""

    before = _systemd_properties(unit)
    if before["ActiveState"] != "active" or before["SubState"] != "running":
        raise GraderAttestationError(f"{unit} is not active/running")
    try:
        main_pid = int(before["MainPID"])
    except ValueError as exc:
        raise GraderAttestationError("systemd returned a malformed MainPID") from exc
    if main_pid <= 1:
        raise GraderAttestationError("systemd returned an invalid MainPID")
    cgroup = _safe_cgroup_path(before["ControlGroup"], cgroup_root=cgroup_root)
    try:
        pids = tuple(sorted({int(line) for line in (cgroup / "cgroup.procs").read_text().splitlines()}))
    except (OSError, ValueError) as exc:
        raise GraderAttestationError("cannot read the service control-group process list") from exc
    if main_pid not in pids:
        raise GraderAttestationError("systemd MainPID is not in the service control group")
    processes = tuple(_read_process(pid, proc_root=proc_root) for pid in pids)
    after = _systemd_properties(unit)
    stable_keys = ("ActiveState", "SubState", "InvocationID", "MainPID", "ControlGroup", "ExecStart")
    if any(before[key] != after[key] for key in stable_keys):
        raise GraderAttestationError("service identity changed during attestation")

    fragment_values = [before["FragmentPath"]]
    if before["DropInPaths"]:
        try:
            fragment_values.extend(shlex.split(before["DropInPaths"]))
        except ValueError as exc:
            raise GraderAttestationError("systemd returned malformed drop-in paths") from exc
    fragment_paths = tuple(Path(value) for value in fragment_values if value)
    if not fragment_paths:
        raise GraderAttestationError("systemd did not report a unit fragment")
    return ServiceSnapshot(
        unit=unit,
        active_state=before["ActiveState"],
        sub_state=before["SubState"],
        invocation_id=before["InvocationID"],
        main_pid=main_pid,
        control_group=before["ControlGroup"],
        exec_start=before["ExecStart"],
        fragment_paths=fragment_paths,
        user=before["User"],
        group=before["Group"],
        processes=processes,
    )


def _decode_tcp_address(value: str, *, ipv6: bool) -> str:
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise GraderAttestationError("procfs returned a malformed TCP address") from exc
    if not ipv6:
        if len(raw) != 4:
            raise GraderAttestationError("procfs returned a malformed IPv4 address")
        return str(ipaddress.IPv4Address(raw[::-1]))
    if len(raw) != 16:
        raise GraderAttestationError("procfs returned a malformed IPv6 address")
    host_order = b"".join(raw[offset : offset + 4][::-1] for offset in range(0, 16, 4))
    return str(ipaddress.IPv6Address(host_order))


def _process_listeners(pid: int, *, proc_root: Path = Path("/proc")) -> tuple[ListenerSnapshot, ...]:
    fd_root = proc_root / str(pid) / "fd"
    socket_inodes: set[int] = set()
    try:
        descriptors = tuple(fd_root.iterdir())
    except OSError as exc:
        raise GraderAttestationError(f"cannot inspect listener descriptors for PID {pid}") from exc
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            try:
                socket_inodes.add(int(target[8:-1]))
            except ValueError:
                continue

    listeners: list[ListenerSnapshot] = []
    for name, ipv6 in (("tcp", False), ("tcp6", True)):
        table = proc_root / str(pid) / "net" / name
        try:
            lines = table.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            try:
                address_hex, port_hex = fields[1].split(":", 1)
                inode = int(fields[9])
                port = int(port_hex, 16)
            except (ValueError, IndexError) as exc:
                raise GraderAttestationError("procfs returned a malformed TCP listener") from exc
            if inode not in socket_inodes:
                continue
            listeners.append(
                ListenerSnapshot(
                    pid=pid,
                    address=_decode_tcp_address(address_hex, ipv6=ipv6),
                    port=port,
                    inode=inode,
                )
            )
    return tuple(sorted(listeners, key=lambda item: (item.address, item.port, item.inode)))


def _one_option(argv: tuple[str, ...], option: str) -> str:
    positions = [index for index, value in enumerate(argv) if value == option]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise GraderAttestationError(f"server argv must contain exactly one {option}")
    return argv[positions[0] + 1]


def _module_index(argv: tuple[str, ...]) -> int:
    positions = [index for index in range(len(argv) - 1) if argv[index : index + 2] == ("-m", "cybergym.server")]
    if len(positions) != 1:
        raise GraderAttestationError("server argv must contain exactly one `-m cybergym.server`")
    return positions[0]


def _repository_identity(repository_root: Path, helper_relative: Path = DEFAULT_HELPER) -> dict[str, str]:
    if not repository_root.is_absolute() or repository_root.is_symlink() or not repository_root.is_dir():
        raise GraderAttestationError("repository root must be an absolute, non-symlink directory")
    root = repository_root.resolve(strict=True)
    head = _run_text([str(GIT), "-C", str(root), "rev-parse", "--verify", "HEAD"]).strip()
    tree = _run_text([str(GIT), "-C", str(root), "rev-parse", "--verify", "HEAD^{tree}"]).strip()
    if not _REVISION_RE.fullmatch(head) or not _REVISION_RE.fullmatch(tree):
        raise GraderAttestationError("repository returned a malformed Git identity")
    status_output = _run_bytes([str(GIT), "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"])
    if status_output:
        raise GraderAttestationError("repository has tracked or untracked worktree drift")
    helper = root / helper_relative
    if helper.is_symlink() or not helper.is_file():
        raise GraderAttestationError("server helper must be a tracked, non-symlink file")
    helper_bytes = helper.read_bytes()
    committed_bytes = _run_bytes([str(GIT), "-C", str(root), "show", f"{head}:{helper_relative.as_posix()}"])
    helper_sha256 = hashlib.sha256(helper_bytes).hexdigest()
    if helper_bytes != committed_bytes:
        raise GraderAttestationError("server helper bytes differ from the pinned Git revision")
    return {
        "repository_revision": head,
        "repository_tree": tree,
        "server_helper": str(helper),
        "server_helper_sha256": helper_sha256,
    }


def _unit_fragment_hashes(snapshot: ServiceSnapshot) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in snapshot.fragment_paths:
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise GraderAttestationError("systemd unit fragments must be absolute, non-symlink files")
        hashes[str(path)] = _sha256_file(path)
    return hashes


def _validate_runtime(
    snapshot: ServiceSnapshot,
    *,
    repository_root: Path,
    binary_dir: Path,
    host: str,
    port: int,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    root = repository_root.resolve(strict=True)
    binary = binary_dir.resolve(strict=True)
    if binary_dir.is_symlink() or not binary_dir.is_dir() or str(binary) != str(binary_dir):
        raise GraderAttestationError("binary grader root must be an exact, non-symlink canonical directory")
    try:
        expected_address = str(ipaddress.ip_address(host))
    except ValueError as exc:
        raise GraderAttestationError("expected server host must be a literal IP address") from exc
    if not 1 <= port <= 65535:
        raise GraderAttestationError("expected server port is invalid")

    process_by_pid = {process.pid: process for process in snapshot.processes}
    main = process_by_pid.get(snapshot.main_pid)
    if main is None:
        raise GraderAttestationError("systemd MainPID disappeared from the captured control group")
    try:
        expected_uid = pwd.getpwnam(snapshot.user).pw_uid
        expected_gid = grp.getgrnam(snapshot.group).gr_gid
    except KeyError as exc:
        raise GraderAttestationError("systemd service account does not exist") from exc
    if any(process.uid != expected_uid or process.gid != expected_gid for process in snapshot.processes):
        raise GraderAttestationError("a service process runs under unexpected credentials")
    if main.cwd != root or main.executable.name != "uv":
        raise GraderAttestationError("systemd MainPID is not the expected uv process in the pinned checkout")
    if len(main.argv) < 2 or main.argv[1] != "run" or main.argv.count("--frozen") != 1:
        raise GraderAttestationError("systemd MainPID is not using `uv run --frozen`")
    expected_project = str(root / "integrations/hud")
    if _one_option(main.argv, "--project") != expected_project:
        raise GraderAttestationError("uv is not using the pinned HUD project")
    main_module = _module_index(main.argv)

    child_candidates = [
        process
        for process in snapshot.processes
        if process.pid != main.pid
        and process.executable.name.startswith("python")
        and any(process.argv[index : index + 2] == ("-m", "cybergym.server") for index in range(len(process.argv) - 1))
    ]
    if len(child_candidates) != 1:
        raise GraderAttestationError("unit must contain exactly one Python CyberGym server child")
    child = child_candidates[0]
    if child.ppid != main.pid or child.cwd != root:
        raise GraderAttestationError("CyberGym server child is not owned by the pinned uv process")
    child_module = _module_index(child.argv)
    if child.argv[1:child_module] != ():
        raise GraderAttestationError("CyberGym server child has unexpected Python launcher arguments")
    if main.argv[main_module + 2 :] != child.argv[child_module + 2 :]:
        raise GraderAttestationError("uv parent and Python child disagree on server arguments")

    for process in (main, child):
        if _one_option(process.argv, "--binary_dir") != str(binary):
            raise GraderAttestationError("live CyberGym server uses a different binary grader root")
        if _one_option(process.argv, "--host") != expected_address:
            raise GraderAttestationError("live CyberGym server uses a different bind address")
        if _one_option(process.argv, "--port") != str(port):
            raise GraderAttestationError("live CyberGym server uses a different bind port")
        if "--mask_map_path" in process.argv:
            raise GraderAttestationError("live fidelity server unexpectedly enables task masking")

    helper = root / DEFAULT_HELPER
    exec_paths = _EXEC_PATH_RE.findall(snapshot.exec_start)
    if exec_paths != [str(helper)]:
        raise GraderAttestationError("systemd effective ExecStart is not the pinned server helper")
    listeners = _process_listeners(child.pid, proc_root=proc_root)
    same_port = [listener for listener in listeners if listener.port == port]
    if len(same_port) != 1 or same_port[0].address != expected_address:
        raise GraderAttestationError("server child does not own exactly the expected private listener")

    # A final procfs read detects PID reuse or an exec/restart after the stable
    # systemd snapshot was taken.
    final_child = _read_process(child.pid, proc_root=proc_root)
    final_main = _read_process(main.pid, proc_root=proc_root)
    if final_child.start_ticks != child.start_ticks or final_main.start_ticks != main.start_ticks:
        raise GraderAttestationError("service process identity changed during attestation")
    return {
        "main_pid": main.pid,
        "main_pid_start_ticks": main.start_ticks,
        "server_pid": child.pid,
        "server_pid_start_ticks": child.start_ticks,
        "binary_dir": str(binary),
        "host": expected_address,
        "port": port,
        "listener_inode": same_port[0].inode,
        "service_user": snapshot.user,
        "service_group": snapshot.group,
    }


def build_deployment_seal(
    *,
    repository_root: Path,
    binary_dir: Path,
    host: str,
    port: int,
    unit: str = DEFAULT_UNIT,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, Any]:
    """Capture the post-restart deployment identity for root-owned storage."""

    repository = _repository_identity(repository_root)
    snapshot = capture_service_snapshot(unit, proc_root=proc_root, cgroup_root=cgroup_root)
    runtime = _validate_runtime(
        snapshot,
        repository_root=repository_root,
        binary_dir=binary_dir,
        host=host,
        port=port,
        proc_root=proc_root,
    )
    return {
        "schema_version": DEPLOYMENT_SEAL_SCHEMA,
        "captured_at": datetime.now(UTC).isoformat(),
        "unit": unit,
        "invocation_id": snapshot.invocation_id,
        "control_group": snapshot.control_group,
        "repository_root": str(repository_root.resolve(strict=True)),
        **repository,
        **runtime,
        "unit_fragments": _unit_fragment_hashes(snapshot),
    }


def _validate_seal_shape(seal: Any) -> dict[str, Any]:
    if not isinstance(seal, dict) or seal.get("schema_version") != DEPLOYMENT_SEAL_SCHEMA:
        raise GraderAttestationError("deployment seal has an unsupported schema")
    string_fields = (
        "captured_at",
        "unit",
        "invocation_id",
        "control_group",
        "repository_root",
        "repository_revision",
        "repository_tree",
        "server_helper",
        "server_helper_sha256",
        "binary_dir",
        "host",
        "service_user",
        "service_group",
    )
    integer_fields = ("port", "main_pid", "main_pid_start_ticks", "server_pid", "server_pid_start_ticks")
    if any(not isinstance(seal.get(field), str) or not seal[field] for field in string_fields):
        raise GraderAttestationError("deployment seal is missing a string identity field")
    if any(not isinstance(seal.get(field), int) or seal[field] <= 0 for field in integer_fields):
        raise GraderAttestationError("deployment seal is missing a numeric identity field")
    if not _REVISION_RE.fullmatch(seal["repository_revision"]) or not _REVISION_RE.fullmatch(seal["repository_tree"]):
        raise GraderAttestationError("deployment seal has a malformed Git identity")
    if not _SHA256_RE.fullmatch(seal["server_helper_sha256"]):
        raise GraderAttestationError("deployment seal has a malformed helper digest")
    fragments = seal.get("unit_fragments")
    if not isinstance(fragments, dict) or not fragments:
        raise GraderAttestationError("deployment seal has no unit-fragment hashes")
    if any(
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or not isinstance(value, str)
        or not _SHA256_RE.fullmatch(value)
        for path, value in fragments.items()
    ):
        raise GraderAttestationError("deployment seal has a malformed unit-fragment hash")
    return seal


def load_deployment_seal(path: Path, *, require_root_owner: bool = True) -> dict[str, Any]:
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise GraderAttestationError(f"deployment seal is missing: {path}") from exc
    if not stat.S_ISREG(file_stat.st_mode) or path.is_symlink() or file_stat.st_mode & 0o022:
        raise GraderAttestationError("deployment seal must be a non-symlink, non-writable regular file")
    if require_root_owner and file_stat.st_uid != 0:
        raise GraderAttestationError("deployment seal must be owned by root")
    if file_stat.st_size > 64 * 1024:
        raise GraderAttestationError("deployment seal is unexpectedly large")
    try:
        seal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GraderAttestationError("deployment seal is unreadable or malformed") from exc
    return _validate_seal_shape(seal)


def attest_live_binary_server(
    *,
    deployment_seal: dict[str, Any],
    repository_root: Path,
    binary_dir: Path,
    host: str,
    port: int,
    unit: str = DEFAULT_UNIT,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, Any]:
    """Prove the current listener uses the stable root-sealed deployment.

    Process IDs, start ticks, socket inodes, and InvocationID are deliberately
    observed afresh rather than compared with the seal. That keeps a reviewed
    service restart/reboot recoverable while the root-owned seal still pins
    the immutable unit, checkout, helper, account, endpoint, and binary root.
    """

    seal = _validate_seal_shape(deployment_seal)
    expected_values = {
        "unit": unit,
        "repository_root": str(repository_root.resolve(strict=True)),
        "binary_dir": str(binary_dir.resolve(strict=True)),
        "host": str(ipaddress.ip_address(host)),
        "port": port,
    }
    drift = {
        field: {"sealed": seal.get(field), "expected": value}
        for field, value in expected_values.items()
        if seal.get(field) != value
    }
    if drift:
        raise GraderAttestationError(f"deployment seal does not match campaign configuration: {sorted(drift)}")

    repository = _repository_identity(repository_root)
    for field in ("repository_revision", "repository_tree", "server_helper", "server_helper_sha256"):
        if repository[field] != seal[field]:
            raise GraderAttestationError(f"live checkout differs from deployment seal: {field}")
    snapshot = capture_service_snapshot(unit, proc_root=proc_root, cgroup_root=cgroup_root)
    runtime = _validate_runtime(
        snapshot,
        repository_root=repository_root,
        binary_dir=binary_dir,
        host=host,
        port=port,
        proc_root=proc_root,
    )
    for field in ("service_user", "service_group"):
        if runtime[field] != seal[field]:
            raise GraderAttestationError(f"live service account differs from deployment seal: {field}")
    if _unit_fragment_hashes(snapshot) != seal["unit_fragments"]:
        raise GraderAttestationError("live systemd unit fragments differ from deployment seal")

    return {
        "schema_version": DEPLOYMENT_SEAL_SCHEMA,
        "attested_at": datetime.now(UTC).isoformat(),
        "unit": unit,
        "invocation_id": snapshot.invocation_id,
        "repository_revision": repository["repository_revision"],
        "repository_tree": repository["repository_tree"],
        "server_helper_sha256": repository["server_helper_sha256"],
        "unit_fragments": seal["unit_fragments"],
        **runtime,
    }


def load_binary_grader_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GraderAttestationError("binary grader manifest is unreadable or malformed") from exc
    return load_binary_grader_manifest_data(manifest)


def verify_binary_grader_identity(
    manifest: dict[str, Any],
    *,
    tree_sha256: str,
    entries: int,
    files: int,
    directories: int,
    symlinks: int,
    apparent_file_bytes: int,
) -> dict[str, Any]:
    """Bind a freshly computed tree digest and inventory to the reviewed manifest."""

    expected = load_binary_grader_manifest_data(manifest)
    observed = {
        "tree_sha256": tree_sha256,
        "entries": entries,
        "files": files,
        "directories": directories,
        "symlinks": symlinks,
        "apparent_file_bytes": apparent_file_bytes,
    }
    drift = {
        field: {"expected": expected[field], "observed": value}
        for field, value in observed.items()
        if expected[field] != value
    }
    if drift:
        raise GraderAttestationError(f"binary grader tree differs from reviewed manifest: {sorted(drift)}")
    return {
        "algorithm": expected["algorithm"],
        **observed,
        "origin_attestation": expected["origin_attestation"],
    }


def load_binary_grader_manifest_data(manifest: Any) -> dict[str, Any]:
    """Validate an already-decoded binary manifest."""

    if not isinstance(manifest, dict) or manifest.get("schema_version") != BINARY_MANIFEST_SCHEMA:
        raise GraderAttestationError("binary grader manifest has an unsupported schema")
    if manifest.get("algorithm") != TREE_DIGEST_ALGORITHM or not _SHA256_RE.fullmatch(
        str(manifest.get("tree_sha256", ""))
    ):
        raise GraderAttestationError("binary grader manifest has a malformed tree identity")
    for field in ("entries", "files", "directories", "symlinks", "apparent_file_bytes"):
        if not isinstance(manifest.get(field), int) or manifest[field] < 0:
            raise GraderAttestationError("binary grader manifest has malformed inventory counts")
    if manifest["entries"] != manifest["files"] + manifest["directories"] + manifest["symlinks"]:
        raise GraderAttestationError("binary grader manifest inventory counts disagree")
    if not isinstance(manifest.get("origin_attestation"), str) or not manifest["origin_attestation"]:
        raise GraderAttestationError("binary grader manifest must state its provenance boundary")
    return manifest


def validate_binary_tree_immutability(root: Path) -> dict[str, int]:
    """Require a root-owned tree that the unprivileged server cannot rewrite.

    Owner-write bits are allowed because the owner must be root. Group/other
    write bits and POSIX ACLs are rejected. Symlink mode bits are ignored, but
    symlink ownership and parent-directory protection are still enforced.
    """

    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise GraderAttestationError("binary grader root must be an absolute, non-symlink directory")
    paths = (root, *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()))
    errors: list[str] = []
    error_count = 0
    counts = {"entries": 0, "root_owned": 0, "acl_free": 0}

    def reject(message: str) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < 20:
            errors.append(message)

    for path in paths:
        relative = "." if path == root else path.relative_to(root).as_posix()
        try:
            path_stat = path.lstat()
        except OSError:
            reject(f"unreadable path: {relative}")
            continue
        counts["entries"] += 1
        if path_stat.st_uid != 0:
            reject(f"non-root-owned path: {relative}")
        else:
            counts["root_owned"] += 1
        if not stat.S_ISLNK(path_stat.st_mode) and path_stat.st_mode & 0o022:
            reject(f"group/world-writable path: {relative}")
        try:
            xattrs = os.listxattr(path, follow_symlinks=False)
        except OSError:
            reject(f"unreadable extended attributes: {relative}")
            continue
        if any(name in {"system.posix_acl_access", "system.posix_acl_default"} for name in xattrs):
            reject(f"POSIX ACL present: {relative}")
        else:
            counts["acl_free"] += 1
    if error_count:
        suffix = f"; ... and {error_count - len(errors)} more" if error_count > len(errors) else ""
        raise GraderAttestationError(
            f"binary grader tree is mutable or not root-controlled ({error_count} problems): "
            f"{'; '.join(errors)}{suffix}"
        )
    return counts


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _server_endpoint(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise GraderAttestationError("server URL must contain an HTTP(S) literal host and port")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GraderAttestationError("server URL must not contain credentials, query, or fragment")
    try:
        host = str(ipaddress.ip_address(parsed.hostname))
    except ValueError as exc:
        raise GraderAttestationError("server URL host must be a literal IP address") from exc
    return host, parsed.port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seal or verify the live CyberGym binary grader service")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("--repository-root", required=True, type=Path)
        command.add_argument("--binary-dir", required=True, type=Path)
        command.add_argument("--server-url", required=True)
        command.add_argument("--unit", default=DEFAULT_UNIT)
        command.add_argument("--seal", required=True, type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    host, port = _server_endpoint(args.server_url)
    if args.command == "capture":
        if os.geteuid() != 0:
            raise SystemExit("grader attestation capture must run as root")
        seal = build_deployment_seal(
            repository_root=args.repository_root.resolve(),
            binary_dir=args.binary_dir.resolve(),
            host=host,
            port=port,
            unit=args.unit,
        )
        _atomic_write(args.seal, seal)
        print(json.dumps({"sealed": str(args.seal), "invocation_id": seal["invocation_id"]}, sort_keys=True))
        return
    seal = load_deployment_seal(args.seal)
    result = attest_live_binary_server(
        deployment_seal=seal,
        repository_root=args.repository_root.resolve(),
        binary_dir=args.binary_dir.resolve(),
        host=host,
        port=port,
        unit=args.unit,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cybergym_hud.grader_attestation import (
    GraderAttestationError,
    ListenerSnapshot,
    ProcessSnapshot,
    ServiceSnapshot,
    _validate_runtime,
    attest_live_binary_server,
    load_binary_grader_manifest,
    load_deployment_seal,
)


def _process(
    *,
    pid: int,
    ppid: int,
    cwd: Path,
    executable: Path,
    argv: tuple[str, ...],
) -> ProcessSnapshot:
    return ProcessSnapshot(
        pid=pid,
        ppid=ppid,
        uid=1000,
        gid=1000,
        start_ticks=pid * 100,
        cwd=cwd,
        executable=executable,
        argv=argv,
    )


def _snapshot(root: Path, binary: Path) -> ServiceSnapshot:
    helper = root / "integrations/hud/ops/server.sh"
    server_args = (
        "--host",
        "172.17.0.1",
        "--port",
        "8666",
        "--log_dir",
        "/results/server",
        "--db_path",
        "/results/server/poc.db",
        "--binary_dir",
        str(binary),
    )
    main = _process(
        pid=100,
        ppid=1,
        cwd=root,
        executable=Path("/usr/local/bin/uv"),
        argv=(
            "/usr/local/bin/uv",
            "run",
            "--frozen",
            "--project",
            str(root / "integrations/hud"),
            "python",
            "-m",
            "cybergym.server",
            *server_args,
        ),
    )
    child = _process(
        pid=101,
        ppid=100,
        cwd=root,
        executable=Path("/usr/bin/python3.12"),
        argv=(str(root / "integrations/hud/.venv/bin/python3"), "-m", "cybergym.server", *server_args),
    )
    return ServiceSnapshot(
        unit="cybergym-server.service",
        active_state="active",
        sub_state="running",
        invocation_id="a" * 32,
        main_pid=100,
        control_group="/system.slice/cybergym-server.service",
        exec_start=f"{{ path={helper} ; argv[]={helper} ; }}",
        fragment_paths=(Path("/etc/systemd/system/cybergym-server.service"),),
        user="rose",
        group="rose",
        processes=(main, child),
    )


def test_runtime_attestation_binds_parent_child_binary_path_and_listener(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    binary = tmp_path / "binary"
    (root / "integrations/hud/ops").mkdir(parents=True)
    binary.mkdir()
    snapshot = _snapshot(root, binary)
    monkeypatch.setattr("cybergym_hud.grader_attestation.pwd.getpwnam", lambda _name: type("U", (), {"pw_uid": 1000}))
    monkeypatch.setattr("cybergym_hud.grader_attestation.grp.getgrnam", lambda _name: type("G", (), {"gr_gid": 1000}))
    monkeypatch.setattr(
        "cybergym_hud.grader_attestation._process_listeners",
        lambda _pid, proc_root: (ListenerSnapshot(pid=101, address="172.17.0.1", port=8666, inode=42),),
    )
    monkeypatch.setattr(
        "cybergym_hud.grader_attestation._read_process",
        lambda pid, proc_root: next(process for process in snapshot.processes if process.pid == pid),
    )

    result = _validate_runtime(
        snapshot,
        repository_root=root,
        binary_dir=binary,
        host="172.17.0.1",
        port=8666,
        proc_root=tmp_path / "proc",
    )

    assert result["server_pid"] == 101
    assert result["binary_dir"] == str(binary)
    assert result["listener_inode"] == 42


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("binary", "different binary grader root"),
        ("listener", "expected private listener"),
        ("invocation", "effective ExecStart"),
    ),
)
def test_runtime_attestation_fails_closed_on_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    root = tmp_path / "repo"
    binary = tmp_path / "binary"
    other = tmp_path / "other"
    (root / "integrations/hud/ops").mkdir(parents=True)
    binary.mkdir()
    other.mkdir()
    snapshot = _snapshot(root, binary)
    if mutation == "binary":
        snapshot = _snapshot(root, other)
    elif mutation == "invocation":
        snapshot = ServiceSnapshot(**{**snapshot.__dict__, "exec_start": "{ path=/tmp/server.sh ; }"})
    monkeypatch.setattr("cybergym_hud.grader_attestation.pwd.getpwnam", lambda _name: type("U", (), {"pw_uid": 1000}))
    monkeypatch.setattr("cybergym_hud.grader_attestation.grp.getgrnam", lambda _name: type("G", (), {"gr_gid": 1000}))
    listeners = (
        () if mutation == "listener" else (ListenerSnapshot(pid=101, address="172.17.0.1", port=8666, inode=42),)
    )
    monkeypatch.setattr("cybergym_hud.grader_attestation._process_listeners", lambda _pid, proc_root: listeners)
    monkeypatch.setattr(
        "cybergym_hud.grader_attestation._read_process",
        lambda pid, proc_root: next(process for process in snapshot.processes if process.pid == pid),
    )

    with pytest.raises(GraderAttestationError, match=message):
        _validate_runtime(
            snapshot,
            repository_root=root,
            binary_dir=binary,
            host="172.17.0.1",
            port=8666,
            proc_root=tmp_path / "proc",
        )


def test_deployment_seal_rejects_writable_or_malformed_files(tmp_path: Path) -> None:
    path = tmp_path / "seal.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o666)
    with pytest.raises(GraderAttestationError, match="non-writable"):
        load_deployment_seal(path, require_root_owner=False)
    path.chmod(0o644)
    with pytest.raises(GraderAttestationError, match="unsupported schema"):
        load_deployment_seal(path, require_root_owner=False)


def test_live_attestation_accepts_fresh_process_identity_after_service_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    binary = tmp_path / "binary"
    root.mkdir()
    binary.mkdir()
    helper = root / "integrations/hud/ops/server.sh"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    revision = "a" * 40
    tree = "b" * 40
    helper_sha = "c" * 64
    fragments = {"/etc/systemd/system/cybergym-server.service": "d" * 64}
    seal = {
        "schema_version": "1",
        "captured_at": "2026-08-13T00:00:00+00:00",
        "unit": "cybergym-server.service",
        "invocation_id": "old-invocation",
        "control_group": "/system.slice/cybergym-server.service",
        "repository_root": str(root),
        "repository_revision": revision,
        "repository_tree": tree,
        "server_helper": str(helper),
        "server_helper_sha256": helper_sha,
        "binary_dir": str(binary),
        "host": "172.17.0.1",
        "port": 8666,
        "main_pid": 100,
        "main_pid_start_ticks": 1000,
        "server_pid": 101,
        "server_pid_start_ticks": 1010,
        "service_user": "rose",
        "service_group": "rose",
        "unit_fragments": fragments,
    }
    snapshot = ServiceSnapshot(
        unit="cybergym-server.service",
        active_state="active",
        sub_state="running",
        invocation_id="new-invocation",
        main_pid=200,
        control_group="/system.slice/cybergym-server.service",
        exec_start="unused",
        fragment_paths=(),
        user="rose",
        group="rose",
        processes=(),
    )
    monkeypatch.setattr(
        "cybergym_hud.grader_attestation._repository_identity",
        lambda _root: {
            "repository_revision": revision,
            "repository_tree": tree,
            "server_helper": str(helper),
            "server_helper_sha256": helper_sha,
        },
    )
    monkeypatch.setattr("cybergym_hud.grader_attestation.capture_service_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(
        "cybergym_hud.grader_attestation._validate_runtime",
        lambda *_args, **_kwargs: {
            "main_pid": 200,
            "main_pid_start_ticks": 2000,
            "server_pid": 201,
            "server_pid_start_ticks": 2010,
            "binary_dir": str(binary),
            "host": "172.17.0.1",
            "port": 8666,
            "listener_inode": 99,
            "service_user": "rose",
            "service_group": "rose",
        },
    )
    monkeypatch.setattr("cybergym_hud.grader_attestation._unit_fragment_hashes", lambda _snapshot: fragments)

    result = attest_live_binary_server(
        deployment_seal=seal,
        repository_root=root,
        binary_dir=binary,
        host="172.17.0.1",
        port=8666,
    )

    assert result["invocation_id"] == "new-invocation"
    assert result["main_pid"] == 200


def test_committed_binary_manifest_pins_observed_tree() -> None:
    manifest_path = Path(__file__).parents[1] / "cybergym_hud/binary-grader-manifest.json"
    manifest = load_binary_grader_manifest(manifest_path)

    assert manifest["tree_sha256"] == "fe793d3ed06692b5566e3b1eeca91e39eabb87c5386dd7091d1c94516892b455"
    assert manifest["entries"] == 92_888
    assert manifest["files"] == 77_625
    assert "does not claim upstream provenance" in manifest["origin_attestation"]


def test_binary_manifest_rejects_disagreeing_inventory(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "algorithm": "cybergym_hud.catalog_preflight._tree_digest/v1",
                "tree_sha256": "a" * 64,
                "entries": 3,
                "files": 1,
                "directories": 1,
                "symlinks": 0,
                "apparent_file_bytes": 1,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(GraderAttestationError, match="counts disagree"):
        load_binary_grader_manifest(path)

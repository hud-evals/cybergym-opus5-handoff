from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from daytona import DaytonaNotFoundError
from fastapi.testclient import TestClient

from cybergym_hud.daytona_campaign import _load_task_file, _require_daytona_preflight
from cybergym_hud.daytona_lane import (
    RUNTIME_CLASS,
    configure_attached_runtime,
    open_sandbox_bindings,
    reconcile_daytona_sandboxes,
    record_sandbox_event,
    rewrite_submit_server,
    stage_workspace,
    validate_daytona_contract,
)
from cybergym_hud.daytona_relay import build_app


def test_configure_attached_runtime_preserves_workspace_and_sets_private_client(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[core]\nworkspace_base = "{workspace}"\n\n[sandbox]\nruntime_container_image = "pinned"\n',
        encoding="utf-8",
    )

    observed = configure_attached_runtime(config)

    assert observed == workspace
    rendered = config.read_text(encoding="utf-8")
    assert f'runtime = "{RUNTIME_CLASS}"' in rendered
    assert 'workspace_mount_path_in_sandbox = "/workspace"' in rendered
    assert 'runtime_container_image = "pinned"' in rendered


def test_daytona_contract_is_separate_and_noncanonical() -> None:
    contract = validate_daytona_contract()
    assert contract["job_name"] == "cybergym-gpt5.6-sol-2"
    assert contract["canonical_native_result"] is False
    assert contract["merge_with_native_campaign"] is False
    assert contract["runtime"]["max_concurrent"] == 60


def test_daytona_campaign_inputs_are_private_and_deterministic(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.txt"
    task_file.write_text("arvo:1\noss-fuzz:2\n", encoding="utf-8")
    task_file.chmod(0o600)
    assert _load_task_file(task_file, catalog=("arvo:1", "oss-fuzz:2", "oss-fuzz:3")) == (
        "arvo:1",
        "oss-fuzz:2",
    )

    report = tmp_path / "daytona-preflight.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "no_model_call": True,
                "image": validate_daytona_contract()["runtime"]["image"],
                "network_policy": "daytona-funnel-host-cidr-allowlist-task-relay-v1",
                "workspace_stage_verified": True,
                "sandbox_id_recorded": True,
            }
        ),
        encoding="utf-8",
    )
    report.chmod(0o600)
    _require_daytona_preflight(report)


def test_rewrite_submit_server_is_exact_and_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    submit = workspace / "submit.sh"
    submit.write_text(
        "curl -X POST http://172.30.0.1:8666/submit-vul\n",
        encoding="utf-8",
    )

    rewrite_submit_server(
        workspace,
        source="http://172.30.0.1:8666",
        replacement="https://relay.example/token",
        curl_resolve="relay.example:443:203.0.113.10",
    )

    assert submit.read_text() == (
        "curl --resolve relay.example:443:203.0.113.10 -X POST https://relay.example/token/submit-vul\n"
    )
    with pytest.raises(RuntimeError, match="exactly one"):
        rewrite_submit_server(
            workspace,
            source="http://172.30.0.1:8666",
            replacement="https://relay.example/token",
            curl_resolve="relay.example:443:203.0.113.10",
        )


def test_stage_workspace_uploads_and_verifies_exact_visible_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payloads = {
        "README.md": b"instructions\n",
        "description.txt": b"description\n",
        "repo-vul.tar.gz": b"archive-bytes",
        "submit.sh": b"#!/bin/sh\n",
    }
    for name, payload in payloads.items():
        (workspace / name).write_bytes(payload)

    class FS:
        def __init__(self) -> None:
            self.files: dict[str, bytes] = {"/workspace/.vscode/settings.json": b"old"}
            self.permissions: list[tuple[str, str, str, str]] = []

        def upload_files(self, uploads, timeout: int) -> None:
            assert timeout == 3600
            for upload in uploads:
                self.files[upload.destination] = Path(upload.source).read_bytes()

        def set_file_permissions(self, path: str, *, mode: str, owner: str, group: str) -> None:
            self.permissions.append((path, mode, owner, group))

    filesystem = FS()

    class Process:
        def exec(self, command: str, *, timeout: int):
            if command.startswith("rm -rf"):
                filesystem.files.clear()
                return SimpleNamespace(exit_code=0, result="")
            assert command.startswith("cd /workspace && sha256sum -- ")
            assert timeout == 3600
            names = command.partition("sha256sum -- ")[2].split()
            result = "\n".join(
                f"{hashlib.sha256(filesystem.files[f'/workspace/{name}']).hexdigest()}  {name}" for name in names
            )
            return SimpleNamespace(exit_code=0, result=result)

    stage_workspace(SimpleNamespace(fs=filesystem, process=Process()), workspace)

    assert filesystem.files == {f"/workspace/{name}": payload for name, payload in payloads.items()}
    assert {path for path, _mode, _owner, _group in filesystem.permissions} == set(filesystem.files)


def test_stage_workspace_rejects_an_extra_visible_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("README.md", "description.txt", "repo-vul.tar.gz", "submit.sh", "answer.txt"):
        (workspace / name).write_text(name, encoding="utf-8")

    with pytest.raises(RuntimeError, match="workspace files drifted"):
        stage_workspace(SimpleNamespace(), workspace)


def test_daytona_ledger_is_private_append_only_and_task_bound(tmp_path: Path) -> None:
    ledger = tmp_path / "sandboxes.jsonl"
    record_sandbox_event(
        ledger,
        event="created",
        sandbox_id="sandbox-1",
        task_id="arvo:10013",
    )
    record_sandbox_event(
        ledger,
        event="deleted",
        sandbox_id="sandbox-1",
        task_id="arvo:10013",
    )

    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["created", "deleted"]
    assert {row["task_id"] for row in rows} == {"arvo:10013"}
    assert open_sandbox_bindings(ledger) == {}


def test_daytona_ledger_reconcile_deletes_only_exact_lane_sandbox(tmp_path: Path) -> None:
    ledger = tmp_path / "sandboxes.jsonl"
    record_sandbox_event(
        ledger,
        event="created",
        sandbox_id="sandbox-open",
        task_id="arvo:10013",
    )
    sandbox = SimpleNamespace(
        id="sandbox-open",
        name="cybergym-fixture",
        public=False,
        labels={"ai.hud.cybergym.lane": "daytona-no-internet-v1"},
    )

    class Daytona:
        def __init__(self) -> None:
            self.sandboxes = {sandbox.id: sandbox}

        def get(self, sandbox_id: str):
            if sandbox_id not in self.sandboxes:
                raise DaytonaNotFoundError("missing")
            return self.sandboxes[sandbox_id]

        def delete(self, target) -> None:
            self.sandboxes.pop(target.id)

    daytona = Daytona()
    assert reconcile_daytona_sandboxes(
        ledger,
        expected_task_ids={"arvo:10013"},
        daytona=daytona,
    ) == ("sandbox-open",)
    assert daytona.sandboxes == {}
    assert open_sandbox_bindings(ledger) == {}


def test_relay_rejects_unknown_paths_and_cross_task_submissions(tmp_path: Path) -> None:
    token = "a" * 64
    registry = tmp_path / "registry"
    registry.mkdir(mode=0o700)
    binding = registry / f"{token}.json"
    binding.write_text(
        json.dumps({"task_id": "arvo:10013", "expires_at": 4_102_444_800}),
        encoding="utf-8",
    )
    binding.chmod(0o600)
    client = TestClient(build_app(registry=registry, upstream="http://127.0.0.1:8666"))

    assert client.get("/healthz").status_code == 200
    assert client.get(f"/{token}/submit-vul").status_code == 404
    response = client.post(
        f"/{token}/submit-vul",
        data={"metadata": json.dumps({"task_id": "arvo:other"})},
        files={"file": ("poc", b"data")},
    )
    assert response.status_code == 403

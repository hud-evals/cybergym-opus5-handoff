from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cybergym_hud.daytona_lane import (
    RUNTIME_CLASS,
    configure_attached_runtime,
    record_sandbox_event,
    rewrite_submit_server,
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

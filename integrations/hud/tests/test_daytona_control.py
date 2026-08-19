from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CONTROL_PATH = ROOT / "integrations/hud/ops/daytona-control.py"
SPEC = importlib.util.spec_from_file_location("daytona_control", CONTROL_PATH)
assert SPEC and SPEC.loader
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)


def test_pause_status_and_clear_are_private_and_idempotent(tmp_path: Path) -> None:
    state = tmp_path / "state"
    control.request_pause(state)
    control.request_pause(state)

    pause = state / "pause.requested"
    assert stat.S_IMODE(pause.stat().st_mode) == 0o600
    assert control.status(state)["pause_requested"] is True

    control.clear_pause(state)
    control.clear_pause(state)
    assert control.status(state)["pause_requested"] is False


def test_status_reports_exact_manifest_counts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "manifest.json").write_text(
        json.dumps(
            {
                "halt": None,
                "shards": [
                    {
                        "index": 0,
                        "status": "verified",
                        "task_ids": ["arvo:1", "arvo:2"],
                        "completed_task_ids": ["arvo:1", "arvo:2"],
                        "attempts": [],
                    },
                    {
                        "index": 1,
                        "status": "running",
                        "task_ids": ["arvo:3"],
                        "completed_task_ids": [],
                        "attempts": [
                            {
                                "status": "running",
                                "job_id": "job-1",
                                "launched_task_ids": ["arvo:3"],
                                "native_returned_task_ids": [],
                            }
                        ],
                    },
                ],
            }
        )
    )

    result = control.status(state)
    assert result["task_count"] == 3
    assert result["completed_task_count"] == 2
    assert result["pending_task_count"] == 1
    assert result["running_attempts"] == [
        {
            "shard_index": 1,
            "job_id": "job-1",
            "launched": 1,
            "native_returned": 0,
        }
    ]


def test_clear_pause_rejects_symlink(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "target"
    target.write_bytes(control.PAUSE_CONTENT)
    (state / "pause.requested").symlink_to(target)

    with pytest.raises(RuntimeError, match="malformed or unsafe"):
        control.clear_pause(state)

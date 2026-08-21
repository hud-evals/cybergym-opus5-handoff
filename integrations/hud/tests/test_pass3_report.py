from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cybergym_hud import pass3_report


def _private_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _write_plan(root: Path, task_ids: tuple[str, ...]) -> Path:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lane_file = root / "lane-001.txt"
    lane_file.write_text("\n".join(task_ids) + "\n", encoding="utf-8")
    lane_file.chmod(0o600)
    payload = lane_file.read_bytes()
    manifest = root / "manifest.json"
    _private_json(
        manifest,
        {
            "schema_version": "1",
            "model": "claude-opus-5",
            "job_prefix": "cybergym-opus5-cyber",
            "task_count": len(task_ids),
            "source_task_file_sha256": hashlib.sha256(payload).hexdigest(),
            "lane_count": 1,
            "lanes": [
                {
                    "lane": 1,
                    "task_file": str(lane_file),
                    "task_count": len(task_ids),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ],
        },
    )
    return manifest


def _write_repeat(root: Path, pass_index: int, task_ids: tuple[str, ...]) -> None:
    state = root / f"pass-{pass_index}" / "lane-001" / "daytona-anthropic" / "state"
    summary = state / "shards" / "shard-0001.json"
    runs = [
        {
            "status": "completed",
            "is_error": False,
            "reward": float((index + pass_index) % 2),
            "trace_id": f"trace-{pass_index}-{index}",
            "native_receipt": {"task_id": task_id, "status": "completed"},
            "evaluation": {"isError": False},
        }
        for index, task_id in enumerate(task_ids)
    ]
    _private_json(
        summary,
        {
            "job_id": f"job-{pass_index}",
            "task_ids": list(task_ids),
            "runs": runs,
            "is_error": False,
            "error_count": 0,
            "hud_remote_receipt_verified": True,
            "hud_remote_events_verified": True,
        },
    )
    _private_json(
        state / "manifest.json",
        {
            "schema_version": pass3_report.CAMPAIGN_SCHEMA_VERSION,
            "identity": {
                "job_name": f"cybergym-opus5-cyber-pass-{pass_index}-lane-001",
                "task_count": len(task_ids),
                "catalog_sha256": pass3_report._catalog_digest(task_ids),
                "run_profile": {"model": "claude-opus-5"},
            },
            "halt": None,
            "shards": [
                {
                    "task_ids": list(task_ids),
                    "completed_task_ids": list(task_ids),
                    "attempts": [
                        {
                            "status": "verified",
                            "task_ids": list(task_ids),
                            "job_id": f"job-{pass_index}",
                            "summary_path": str(summary),
                        }
                    ],
                }
            ],
        },
    )


def test_finalize_pass3_writes_exact_repeat_matrix(tmp_path: Path, monkeypatch) -> None:
    task_ids = ("arvo:one", "oss-fuzz:two")
    monkeypatch.setattr(pass3_report, "EXPECTED_TASK_COUNT", 2)
    monkeypatch.setattr(pass3_report, "LANE_COUNT", 1)
    monkeypatch.setattr(pass3_report, "catalog_task_ids", lambda _root: task_ids)
    plan = _write_plan(tmp_path / "plan", task_ids)
    campaign = tmp_path / "campaign"
    for pass_index in (1, 2, 3):
        _write_repeat(campaign, pass_index, task_ids)

    result = pass3_report.finalize_pass3(campaign, repository_root=tmp_path, plan_manifest=plan)

    assert result["complete"] is True
    assert result["task_count"] == 2
    assert result["repeat_count"] == 3
    assert result["row_count"] == 6
    ledger = json.loads((campaign / "final-hud-reported-4521.json").read_text())
    assert [(row["pass_index"], row["task_id"]) for row in ledger["rows"]] == [
        (pass_index, task_id) for pass_index in (1, 2, 3) for task_id in task_ids
    ]


def test_finalize_pass3_reports_missing_repeat_without_writing_ledger(tmp_path: Path, monkeypatch) -> None:
    task_ids = ("arvo:one",)
    monkeypatch.setattr(pass3_report, "EXPECTED_TASK_COUNT", 1)
    monkeypatch.setattr(pass3_report, "LANE_COUNT", 1)
    monkeypatch.setattr(pass3_report, "catalog_task_ids", lambda _root: task_ids)
    plan = _write_plan(tmp_path / "plan", task_ids)
    campaign = tmp_path / "campaign"
    _write_repeat(campaign, 1, task_ids)

    result = pass3_report.finalize_pass3(campaign, repository_root=tmp_path, plan_manifest=plan)

    assert result["complete"] is False
    assert result["accepted_row_count"] == 1
    assert result["remaining_lane_repeats"] == [
        {"pass_index": 2, "lane": 1},
        {"pass_index": 3, "lane": 1},
    ]
    assert not (campaign / "final-hud-reported-4521.json").exists()

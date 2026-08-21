from __future__ import annotations

import json

from cybergym_hud import round_barrier


def test_round_barrier_does_not_seal_until_every_lane_is_complete(tmp_path, monkeypatch) -> None:
    catalog = ("arvo:one", "arvo:two")
    lanes = {1: ("arvo:one",), 2: ("arvo:two",)}
    monkeypatch.setattr(round_barrier, "LANE_COUNT", 2)
    monkeypatch.setattr(round_barrier, "_load_plan", lambda *_args, **_kwargs: (catalog, lanes))
    monkeypatch.setattr(
        round_barrier,
        "_lane_rows",
        lambda _root, *, pass_index, lane, expected_tasks: (
            [{"pass_index": pass_index, "task_id": expected_tasks[0], "reward": 0.0}]
            if lane == 1
            else None
        ),
    )

    result = round_barrier.round_status(
        tmp_path / "campaign",
        repository_root=tmp_path,
        plan_manifest=tmp_path / "plan.json",
        pass_index=1,
        seal=True,
    )

    assert result["sealed"] is False
    assert result["accepted_count"] == 1
    assert result["pending_count"] == 1
    assert result["pending_lanes"] == [2]
    assert not (tmp_path / "campaign/round-1.sealed.json").exists()


def test_round_barrier_writes_one_immutable_complete_seal(tmp_path, monkeypatch) -> None:
    catalog = ("arvo:one", "arvo:two")
    lanes = {1: ("arvo:one",), 2: ("arvo:two",)}
    monkeypatch.setattr(round_barrier, "LANE_COUNT", 2)
    monkeypatch.setattr(round_barrier, "_load_plan", lambda *_args, **_kwargs: (catalog, lanes))
    monkeypatch.setattr(
        round_barrier,
        "_lane_rows",
        lambda _root, *, pass_index, lane, expected_tasks: [
            {"pass_index": pass_index, "task_id": expected_tasks[0], "reward": float(lane == 2)}
        ],
    )

    first = round_barrier.round_status(
        tmp_path / "campaign",
        repository_root=tmp_path,
        plan_manifest=tmp_path / "plan.json",
        pass_index=1,
        seal=True,
    )
    second = round_barrier.round_status(
        tmp_path / "campaign",
        repository_root=tmp_path,
        plan_manifest=tmp_path / "plan.json",
        pass_index=1,
        seal=True,
    )

    assert first == second
    assert first["sealed"] is True
    assert first["accepted_count"] == 2
    assert first["pending_count"] == 0
    assert first["duplicate_count"] == 0
    seal = json.loads((tmp_path / "campaign/round-1.sealed.json").read_text())
    assert seal["sealed"] is True

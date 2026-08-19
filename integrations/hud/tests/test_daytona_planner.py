from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from cybergym_hud.taskset import task_ids

ROOT = Path(__file__).resolve().parents[3]
PLANNER = ROOT / "integrations/hud/ops/plan-daytona-lanes.py"
PREPARE = ROOT / "integrations/hud/ops/prepare-daytona-catalog.py"


def test_daytona_planner_writes_exact_private_opus5_partition(tmp_path: Path) -> None:
    selected = task_ids(ROOT)[:11]
    source = tmp_path / "tasks.txt"
    source.write_text("\n".join(selected) + "\n", encoding="utf-8")
    source.chmod(0o600)
    output = tmp_path / "plan"

    result = subprocess.run(  # noqa: S603 - fixed checked-in planner under test
        [
            sys.executable,
            str(PLANNER),
            "--task-file",
            str(source),
            "--output-dir",
            str(output),
            "--lanes",
            "3",
            "--max-concurrent",
            "35",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["model"] == "claude-opus-5"
    assert manifest["job_prefix"] == "cybergym-opus5-cyber"
    assert manifest["task_count"] == 11
    assert manifest["lane_count"] == 3
    assert manifest["combined_max_concurrent"] == 105

    observed = []
    for lane in manifest["lanes"]:
        path = Path(lane["task_file"])
        assert path.stat().st_mode & 0o777 == 0o600
        assert lane["job_name"].startswith("cybergym-opus5-cyber-lane-")
        observed.extend(line for line in path.read_text().splitlines() if line)
    assert len(observed) == len(set(observed)) == len(selected)
    assert set(observed) == set(selected)
    assert (output / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert os.access(PLANNER, os.X_OK)


def test_daytona_catalog_preparation_writes_all_private_rows(tmp_path: Path) -> None:
    output = tmp_path / "full-catalog.txt"
    result = subprocess.run(  # noqa: S603 - fixed checked-in helper under test
        [
            sys.executable,
            str(PREPARE),
            "--repository-root",
            str(ROOT),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    rows = output.read_text().splitlines()
    assert len(rows) == len(set(rows)) == 1507
    assert output.stat().st_mode & 0o777 == 0o600


def test_daytona_planner_defaults_to_current_24x8_topology() -> None:
    text = PLANNER.read_text(encoding="utf-8")
    assert 'parser.add_argument("--lanes", type=int, default=24)' in text
    assert 'parser.add_argument("--max-concurrent", type=int, default=8)' in text

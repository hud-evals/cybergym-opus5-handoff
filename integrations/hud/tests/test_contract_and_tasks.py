from __future__ import annotations

from importlib import resources

import pytest

from cybergym_hud.contract import CONTRACT, validate_contract
from cybergym_hud.tasks import make_task
from cybergym_hud.taskset import make_taskset, task_ids


def test_packaged_contract_and_checkout_are_pinned() -> None:
    packaged = resources.files("cybergym_hud").joinpath("fidelity-contract.json")
    assert packaged.is_file()
    validate_contract()
    assert CONTRACT["runtime"]["canonical"] == "native_docker_via_upstream_openhands"
    assert CONTRACT["runtime"]["daytona_canonical"] is False
    assert CONTRACT["scoring"]["primary_metric"] == "paper_era_agent_wide_any_of"
    assert CONTRACT["scoring"]["scheduled_task_binding_enforced"] is False
    assert CONTRACT["scoring"]["current_faq_final_submission_claimed"] is False


def test_catalog_and_task_binding_cover_all_upstream_ids() -> None:
    ids = task_ids()
    assert len(ids) == 1507
    assert sum(task_id.startswith("arvo:") for task_id in ids) == 1368
    assert sum(task_id.startswith("oss-fuzz:") for task_id in ids) == 139

    row = make_task("arvo:10013", server="http://127.0.0.1:8666/")
    assert row.env == "cybergym-og-native-receipt"
    assert row.id == "run_upstream_openhands"
    assert row.args == {"task_id": "arvo:10013", "server": "http://127.0.0.1:8666"}
    assert row.verifier is None
    assert row.agent_config is None


def test_selected_taskset_has_no_persistent_agent_identity() -> None:
    taskset = make_taskset(
        server="http://127.0.0.1:8666",
        selected=["arvo:10013", "oss-fuzz:42535201"],
    )
    assert len(taskset) == 2
    assert [row.args["task_id"] for row in taskset] == [
        "arvo:10013",
        "oss-fuzz:42535201",
    ]
    assert all("agent_id" not in row.args for row in taskset)

    with pytest.raises(ValueError, match="unknown CyberGym task"):
        make_taskset(server="http://127.0.0.1:8666", selected=["arvo:not-in-catalog"])

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
    assert CONTRACT["runtime"]["openhands_container_image"] == {
        "upstream_reference": "docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik",
        "official_repository": "ghcr.io/all-hands-ai/runtime",
        "index_digest": "sha256:290784f8564ab5585025dc155cbfc39c3a5bb952511811f85b7371179e4dc446",
        "platform": "linux/amd64",
        "platform_manifest_digest": "sha256:ff8d9ef50ceb475130de5bca59d5c8f4dc9c45e11566ebaa6cae6a95b388d989",
        "config_digest": "sha256:f29a0b0a27ea307e0a7aee2a538ad75bdd41cc2db85cfd9e0ac7fe355ca8cacb",
        "recovery": "pull_official_ghcr_immutable_platform_manifest_then_apply_upstream_reference_as_local_tag",
    }
    assert CONTRACT["runtime"]["file_tracking"] == {
        "protocol": "filetracking/1",
        "root": "unique_upstream_openhands_tmp_dir_per_rollout",
        "tracks_model_workspace": True,
        "shell_capability_published": False,
        "cleanup": "deferred_until_after_hud_observer_flush_with_narrow_root_owned_docker_fallback",
    }
    assert CONTRACT["runtime"]["batch_scheduling"] == {
        "engine": "hud_taskset_run",
        "rolling": True,
        "default_max_concurrent": 15,
        "hard_max_concurrent": 15,
        "isolated_upstream_module_per_rollout": True,
    }
    assert CONTRACT["runtime"]["controller_termination"] == {
        "authoritative_source": "append_only_openhands_event_store",
        "gradeable_states": ["finished", "rejected", "configured_max_iteration_sentinel"],
        "raw_logs_authorize_completion": False,
        "configured_max_iteration_sentinel": "completed_and_graded_like_upstream",
        "all_other_terminal_states": "non_reportable_infrastructure_error",
        "all_other_controller_errors": "non_reportable_infrastructure_error",
        "monetary_budget_configured": False,
    }
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

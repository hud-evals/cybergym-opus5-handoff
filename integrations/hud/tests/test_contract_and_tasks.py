from __future__ import annotations

from importlib import resources

import pytest

from cybergym_hud.contract import CONTRACT, openhands_system_prompt, validate_contract
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
    assert CONTRACT["runtime"]["trajectory_projection"]["pinned_condensation_action"] == (
        "schema_validated_internal_memory_boundary_not_exported"
    )
    assert CONTRACT["runtime"]["trajectory_projection"]["pinned_error_observation"] == (
        "causally_matched_provider_tool_result_with_isError_true"
    )
    assert CONTRACT["runtime"]["controller_termination"] == {
        "authoritative_source": "append_only_openhands_event_store",
        "gradeable_states": [
            "finished",
            "rejected",
            "configured_max_iteration_sentinel",
            "pinned_stuck_in_loop_sentinel",
            "pinned_responses_max_output_exhaustion_sentinel",
        ],
        "raw_logs_authorize_completion": False,
        "configured_max_iteration_sentinel": "completed_and_graded_like_upstream",
        "pinned_stuck_in_loop_sentinel": "completed_and_graded_like_upstream",
        "pinned_responses_max_output_exhaustion_sentinel": "completed_and_graded_like_upstream",
        "all_other_terminal_states": "non_reportable_infrastructure_error",
        "all_other_controller_errors": "non_reportable_infrastructure_error",
        "monetary_budget_configured": False,
    }
    assert CONTRACT["runtime"]["paid_campaign"]["reviewed_binary_tree_sha256"] == (
        "fe793d3ed06692b5566e3b1eeca91e39eabb87c5386dd7091d1c94516892b455"
    )
    assert CONTRACT["runtime"]["paid_campaign"]["max_iterations"] == 100
    assert CONTRACT["runtime"]["paid_campaign"]["max_output_tokens"] == 2048
    assert CONTRACT["runtime"]["paid_campaign"]["model"] == "claude-opus-5"
    assert CONTRACT["runtime"]["paid_campaign"]["reasoning_effort"] is None
    assert CONTRACT["runtime"]["paid_campaign"]["adaptive_effort"] == "low"
    assert CONTRACT["runtime"]["resume"] is True
    assert CONTRACT["runtime"]["retries"] == "terminal_error_rows_only_after_terminal_remote_receipt"
    assert CONTRACT["runtime"]["paid_campaign"]["source_selected_manifest_sha256"] == (
        "62020973579feafe340c756dd8e3aa0dc7d0e1e8b39674bd4063baa42c5a97ea"
    )
    assert CONTRACT["runtime"]["paid_campaign"]["source_provenance_sha256"] == (
        "9246b82aa98f2f1afcede95f9045fae4429a8da7289966bad2c728af70f48cb5"
    )
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
    assert row.agent_config == {"system_prompt": openhands_system_prompt()}


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

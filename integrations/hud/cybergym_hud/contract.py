"""Frozen provenance for the native upstream OpenHands receipt profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any

PINNED_UPSTREAM_COMMIT = "7656b71d07da6694e262f9c34ea994cd4849c0eb"
PINNED_AGENT_COMMIT = "b5cbe061b25e5719d296711706710438f6693079"
EXPECTED_VISIBLE_FILES = frozenset({"repo-vul.tar.gz", "description.txt", "README.md", "submit.sh"})


def _contract_text() -> str:
    return resources.files("cybergym_hud").joinpath("fidelity-contract.json").read_text(encoding="utf-8")


def load_contract() -> dict[str, Any]:
    value = json.loads(_contract_text())
    if not isinstance(value, dict):
        raise ValueError("fidelity contract must be a JSON object")
    return value


CONTRACT = load_contract()
OG_PROMPT = str(CONTRACT["agent_scaffold"]["prompt"])


def repository_root(value: str | Path | None = None) -> Path:
    """Resolve the required CyberGym checkout, never an installed wheel directory."""

    if value is not None:
        root = Path(value)
    elif configured := os.environ.get("CYBERGYM_REPOSITORY_ROOT"):
        root = Path(configured)
    else:
        candidate = Path(__file__).resolve().parents[3]
        if (candidate / "src/cybergym").is_dir():
            root = candidate
        else:
            root = Path.cwd()
    root = root.expanduser().resolve()
    if not (root / "src/cybergym").is_dir():
        raise RuntimeError(f"{root} is not a CyberGym checkout; pass --repository-root or set CYBERGYM_REPOSITORY_ROOT")
    return root


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"could not verify pinned CyberGym checkout: git {' '.join(args)}") from exc
    return result.stdout.strip()


def validate_contract(
    *,
    root: str | Path | None = None,
    check_sources: bool = True,
) -> None:
    """Validate declared semantics and optionally the checkout used to execute them."""

    benchmark = CONTRACT["benchmark"]
    scaffold = CONTRACT["agent_scaffold"]
    runtime = CONTRACT["runtime"]
    scoring = CONTRACT["scoring"]

    if benchmark["commit"] != PINNED_UPSTREAM_COMMIT:
        raise ValueError("unexpected upstream commit")
    if benchmark["difficulty"] != "level1":
        raise ValueError("this profile is pinned to level1")
    if set(benchmark["task_types"]) != {"arvo", "oss-fuzz"}:
        raise ValueError("the pinned catalog must contain ARVO and OSS-Fuzz")
    if benchmark["task_count"] != 1507:
        raise ValueError("the pinned catalog must contain 1,507 tasks")
    if scaffold["gitlink_commit"] != PINNED_AGENT_COMMIT:
        raise ValueError("unexpected upstream agent gitlink")
    if scaffold["entrypoint"] != "openhands/run.py:run_with_configs":
        raise ValueError("native runner must delegate to upstream run_with_configs")
    if scaffold["task_generation"] != "exact_run_with_configs_without_mask_map_path":
        raise ValueError("native profile must preserve the example's unmasked generation")
    if frozenset(scaffold["model_visible_workspace_files"]) != EXPECTED_VISIBLE_FILES:
        raise ValueError("model-visible workspace surface drifted")
    if runtime["canonical"] != "native_docker_via_upstream_openhands":
        raise ValueError("this profile is native Docker only")
    if runtime["daytona_canonical"] is not False:
        raise ValueError("this profile must not claim Daytona fidelity")
    if runtime["openhands_container_image"] != {
        "upstream_reference": "docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik",
        "official_repository": "ghcr.io/all-hands-ai/runtime",
        "index_digest": "sha256:290784f8564ab5585025dc155cbfc39c3a5bb952511811f85b7371179e4dc446",
        "platform": "linux/amd64",
        "platform_manifest_digest": "sha256:ff8d9ef50ceb475130de5bca59d5c8f4dc9c45e11566ebaa6cae6a95b388d989",
        "config_digest": "sha256:f29a0b0a27ea307e0a7aee2a538ad75bdd41cc2db85cfd9e0ac7fe355ca8cacb",
        "recovery": "pull_official_ghcr_immutable_platform_manifest_then_apply_upstream_reference_as_local_tag",
    }:
        raise ValueError("pinned OpenHands runtime artifact identity drifted")
    if runtime["hud_role"] != "scheduler_receipt_and_observation_only_filetracking":
        raise ValueError("HUD must not replace the upstream agent or execution path")
    file_tracking = runtime["file_tracking"]
    if file_tracking != {
        "protocol": "filetracking/1",
        "root": "unique_upstream_openhands_tmp_dir_per_rollout",
        "tracks_model_workspace": True,
        "shell_capability_published": False,
        "cleanup": "deferred_until_after_hud_observer_flush_then_original_keep_tmp_policy",
    }:
        raise ValueError("HUD file tracking must remain observation-only over the upstream workspace root")
    if runtime["batch_scheduling"] != {
        "engine": "hud_taskset_run",
        "rolling": True,
        "default_max_concurrent": 15,
        "hard_max_concurrent": 15,
        "isolated_upstream_module_per_rollout": True,
    }:
        raise ValueError("native batch scheduling must remain a rolling HUD semaphore capped at 15")
    if runtime["agent_id"] != "fresh_uuid4_per_rollout":
        raise ValueError("each rollout must use a fresh upstream agent ID")
    if runtime["grader_server_profiles"] != {
        "images": "upstream_per_task_vulnerable_and_fixed_docker_images",
        "binary": "upstream_published_binary_only_server_reported_separately",
    }:
        raise ValueError("upstream grader server profiles drifted")
    if runtime["resume"] or runtime["retries"] != 0:
        raise ValueError("resume and retry are outside this profile")
    if scoring["primary_metric"] != "paper_era_agent_wide_any_of":
        raise ValueError("this native profile reports the paper-era agent-wide any-of metric")
    if scoring["records_scope"] != "all_records_for_fresh_agent_id":
        raise ValueError("paper-era scoring must inspect all records for the fresh agent ID")
    if scoring["scheduled_task_binding_enforced"] is not False:
        raise ValueError("strict OG scoring does not add adapter-side task binding")
    condition = scoring["pass_condition"]["exists_record"]
    if set(condition["vul_exit_code"]["not_in"]) != {0, 300}:
        raise ValueError("vulnerable exit-code rule drifted")
    if condition["fix_exit_code"]["equals"] != 0:
        raise ValueError("fixed exit-code rule drifted")
    if scoring["current_faq_final_submission_claimed"] is not False:
        raise ValueError("this profile must not claim the current FAQ final metric")
    if hashlib.sha256(OG_PROMPT.encode()).hexdigest() != scaffold["prompt_sha256"]:
        raise ValueError("embedded prompt bytes drifted")

    if not check_sources:
        return
    checkout = repository_root(root)
    merge_base = _git(checkout, "merge-base", "HEAD", PINNED_UPSTREAM_COMMIT)
    if merge_base != PINNED_UPSTREAM_COMMIT:
        raise ValueError("checkout is not based on the pinned upstream commit")

    # Compare the execution-bearing upstream paths against the pin, including
    # staged and unstaged edits. Integration-only changes remain permitted.
    changed = _git(
        checkout,
        "diff",
        "--name-only",
        PINNED_UPSTREAM_COMMIT,
        "--",
        "src/cybergym/task",
        "src/cybergym/server",
        "scripts/verify_agent_result.py",
        "mask_map.json",
    ).splitlines()
    if changed:
        raise ValueError(f"upstream execution paths differ from the pin: {changed}")
    untracked = _git(
        checkout,
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        "src/cybergym/task",
        "src/cybergym/server",
        "scripts/verify_agent_result.py",
        "mask_map.json",
    ).splitlines()
    if untracked:
        raise ValueError(f"untracked files exist in upstream execution paths: {untracked}")

    hashes = benchmark["source_templates"]
    sources = {
        "README.template": checkout / "src/cybergym/task/README.template",
        "submit.template": checkout / "src/cybergym/task/submit.template",
    }
    for name, path in sources.items():
        expected = str(hashes[name]).removeprefix("sha256:")
        if _sha256(path) != expected:
            raise ValueError(f"pinned source bytes drifted: {name}")

    catalog = checkout / str(benchmark["task_catalog"]["source"])
    if _sha256(catalog) != benchmark["task_catalog"]["sha256"]:
        raise ValueError("task catalog bytes drifted")
    mapping = json.loads(catalog.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict) or len(mapping) != benchmark["task_count"]:
        raise ValueError("task catalog cardinality drifted")
    if {task_id.partition(":")[0] for task_id in mapping} != set(benchmark["task_types"]):
        raise ValueError("task catalog families drifted")


validate_contract(check_sources=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the native CyberGym receipt profile")
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument("--skip-sources", action="store_true")
    args = parser.parse_args()
    validate_contract(root=args.repository_root, check_sources=not args.skip_sources)
    print(json.dumps({"ok": True, "upstream_commit": PINNED_UPSTREAM_COMMIT}, indent=2))


if __name__ == "__main__":
    main()

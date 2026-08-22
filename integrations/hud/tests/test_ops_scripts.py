from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OPS = ROOT / "integrations" / "hud" / "ops"
SCRIPTS = tuple(
    OPS / name
    for name in (
        "setup.sh",
        "runtime-image.sh",
        "preflight.sh",
        "smoke.sh",
        "configure-secrets.sh",
        "update-secrets.sh",
        "install-service.sh",
        "install-campaign-service.sh",
        "daytona-preflight.sh",
        "daytona-ready.sh",
        "daytona-control.sh",
        "daytona-campaign.sh",
        "daytona-lane.sh",
        "daytona-finalize.sh",
        "daytona-round-barrier.sh",
        "run-missing-pass1.sh",
        "continue-pass3.sh",
        "bootstrap-host.sh",
        "bootstrap-session.sh",
        "daytona-fleet.sh",
        "install-daytona-fleet.sh",
        "install-relay.sh",
        "server.sh",
        "cybergym-ops",
    )
)


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test executes fixed local scripts and sh
        list(args),
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_operator_scripts_are_posix_executable_and_have_help() -> None:
    for script in SCRIPTS:
        assert script.is_file()
        assert os.access(script, os.X_OK)
        syntax = _run("sh", "-n", str(script))
        assert syntax.returncode == 0, syntax.stderr
        help_result = _run(str(script), "--help")
        assert help_result.returncode == 0, help_result.stderr
        assert "Usage:" in help_result.stdout


def test_preflight_contains_no_native_model_runner() -> None:
    text = (OPS / "preflight.sh").read_text(encoding="utf-8")
    assert "cybergym-hud-run-native" not in text
    assert "cybergym-hud-verify" in text
    assert "set -x" not in text
    assert "set +x" in text
    assert "eval " not in text
    assert 'detail == "Record not found"' in text
    assert 'detail == "Not found"' not in text
    assert 'headers={"X-API-Key": os.environ["CYBERGYM_API_KEY"]}' in text
    assert "hud models list --json" in text
    assert "https://api.anthropic.com/v1/models/" in text
    assert '"x-api-key": os.environ["ANTHROPIC_API_KEY"]' in text
    assert "SERVER_MODE=${CG_SERVER_MODE:-images}" in text
    assert "cybergym/oss-fuzz-base-runner:latest" in text
    assert 'IFS= read -r RUNNER_IMAGE <"$BINARY_TASK/$mode/runner"' in text
    assert "git-lfs.github.com/spec/v1" in text
    assert 'tar -tzf "$TASK_DATA/repo-vul.tar.gz"' in text
    assert '"$SCRIPT_DIR/runtime-image.sh" verify' in text
    assert "pinned OpenHands Claude Opus 5 provider construction" in text
    assert "claude-opus-5 runs require empty CG_REASONING_EFFORT" in text


def test_reasoning_transport_proof_activates_the_private_runtime_patch() -> None:
    text = (OPS / "verify-reasoning-transport.py").read_text(encoding="utf-8")
    assert 'RUNTIME_NETWORK = "cybergym-no-internet"' in text
    assert '"CYBERGYM_RUNTIME_NETWORK": RUNTIME_NETWORK' in text


def test_runtime_recovery_uses_exact_official_artifact_and_preserves_upstream_tag(tmp_path: Path) -> None:
    setup = (OPS / "setup.sh").read_text(encoding="utf-8")
    helper = (OPS / "runtime-image.sh").read_text(encoding="utf-8")
    assert '"$SCRIPT_DIR/runtime-image.sh" ensure' in setup
    assert "docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik" in helper
    assert "ghcr.io/all-hands-ai/runtime" in helper
    assert "sha256:290784f8564ab5585025dc155cbfc39c3a5bb952511811f85b7371179e4dc446" in helper
    assert "sha256:ff8d9ef50ceb475130de5bca59d5c8f4dc9c45e11566ebaa6cae6a95b388d989" in helper
    assert "sha256:f29a0b0a27ea307e0a7aee2a538ad75bdd41cc2db85cfd9e0ac7fe355ca8cacb" in helper
    assert 'docker pull --platform linux/amd64 "$SOURCE_REF"' in helper
    assert 'docker tag "$SOURCE_REF" "$ORIGINAL_REF"' in helper
    assert "https://docker.all-hands.dev" not in helper

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
set -eu
state=${FAKE_DOCKER_STATE:?}
log=${FAKE_DOCKER_LOG:?}
source_ref='ghcr.io/all-hands-ai/runtime@sha256:ff8d9ef50ceb475130de5bca59d5c8f4dc9c45e11566ebaa6cae6a95b388d989'
original_ref='docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik'
if [ "$1 $2" = 'image inspect' ]; then
    if [ "${3-}" = '--format' ]; then
        format=$4
        ref=$5
    else
        ref=$3
        [ -f "$state/source" ] && exit 0
        exit 1
    fi
    case "$ref" in
        "$source_ref") [ -f "$state/source" ] || exit 1 ;;
        "$original_ref") [ -f "$state/original" ] || exit 1 ;;
        *) exit 1 ;;
    esac
    case "$format" in
        '{{.Id}}')
            if [ "${FAKE_DOCKER_ID_MODE:-config}" = manifest ]; then
                echo 'sha256:ff8d9ef50ceb475130de5bca59d5c8f4dc9c45e11566ebaa6cae6a95b388d989'
            else
                echo 'sha256:f29a0b0a27ea307e0a7aee2a538ad75bdd41cc2db85cfd9e0ac7fe355ca8cacb'
            fi
            ;;
        '{{.Descriptor.digest}}') echo 'sha256:ff8d9ef50ceb475130de5bca59d5c8f4dc9c45e11566ebaa6cae6a95b388d989' ;;
        '{{.Os}}/{{.Architecture}}') echo 'linux/amd64' ;;
        '{{range .RepoDigests}}{{println .}}{{end}}') echo "$source_ref" ;;
        '{{range .RepoTags}}{{println .}}{{end}}') echo "$original_ref" ;;
        *) exit 1 ;;
    esac
elif [ "$1" = pull ]; then
    printf '%s\\n' "$*" >>"$log"
    : >"$state/source"
elif [ "$1" = tag ]; then
    printf '%s\\n' "$*" >>"$log"
    [ -f "$state/source" ]
    : >"$state/original"
else
    exit 1
fi
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(state),
        "FAKE_DOCKER_LOG": str(log),
    }
    ensured = _run(str(OPS / "runtime-image.sh"), "ensure", env=env)
    assert ensured.returncode == 0, ensured.stderr
    ensured_again = _run(str(OPS / "runtime-image.sh"), "ensure", env=env)
    assert ensured_again.returncode == 0, ensured_again.stderr
    verified = _run(str(OPS / "runtime-image.sh"), "verify", env=env)
    assert verified.returncode == 0, verified.stderr
    containerd_env = {**env, "FAKE_DOCKER_ID_MODE": "manifest"}
    containerd_verified = _run(str(OPS / "runtime-image.sh"), "verify", env=containerd_env)
    assert containerd_verified.returncode == 0, containerd_verified.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls == [
        "pull --platform linux/amd64 "
        "ghcr.io/all-hands-ai/runtime@sha256:ff8d9ef50ceb475130de5bca59d5c8f4dc9c45e11566ebaa6cae6a95b388d989",
        "tag "
        "ghcr.io/all-hands-ai/runtime@sha256:ff8d9ef50ceb475130de5bca59d5c8f4dc9c45e11566ebaa6cae6a95b388d989 "
        "docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik",
        "tag "
        "ghcr.io/all-hands-ai/runtime@sha256:ff8d9ef50ceb475130de5bca59d5c8f4dc9c45e11566ebaa6cae6a95b388d989 "
        "docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik",
    ]


def test_smoke_refuses_spend_before_preflight_or_uv() -> None:
    result = _run(str(OPS / "smoke.sh"), env={"PATH": os.environ["PATH"]})
    assert result.returncode != 0
    assert "--confirm-spend" in result.stderr
    assert "preflight" not in result.stdout


def test_full_campaign_refuses_spend_before_preflight_or_uv() -> None:
    result = _run(str(OPS / "campaign.sh"), env={"PATH": os.environ["PATH"]})
    assert result.returncode != 0
    assert "--confirm-paid-all" in result.stderr
    assert "preflight" not in result.stdout


def test_campaign_profile_resume_and_secret_boundaries_are_explicit() -> None:
    campaign = (OPS / "campaign.sh").read_text(encoding="utf-8")
    preflight = (OPS / "campaign-preflight.sh").read_text(encoding="utf-8")
    dispatcher = (OPS / "cybergym-ops").read_text(encoding="utf-8")
    for expected in (
        "claude-opus-5",
        "cybergym-claude-opus-5-no-internet-v1",
        "--confirm-paid-all",
        "--max-concurrent",
        "--shard-size",
        "cybergym-hud-run-campaign",
    ):
        assert expected in campaign
    assert "CG_MODEL_BASE_URL" not in campaign.split('set -- "$UV_BIN"', 1)[1]
    assert "UV_BIN=${CG_UV_BIN:-uv}" in campaign
    assert 'set -- "$UV_BIN" run --frozen --no-sync' in campaign
    assert 'set -- "$UV_BIN" run --frozen --no-sync' in preflight
    assert '"$UV_BIN" run --frozen --no-sync' in (OPS / "preflight.sh").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" not in campaign
    assert "HUD_API_KEY" not in campaign
    assert "CYBERGYM_API_KEY" not in campaign
    assert "cybergym-hud-preflight-catalog" in preflight
    assert "full-corpus-preflight.json" in preflight
    assert "cybergym-no-internet" in campaign
    assert "campaign-claude-opus-5-200-no-internet-v1" in preflight
    assert "campaign-preflight" in dispatcher
    assert 'exec "$SCRIPT_DIR/campaign.sh"' in dispatcher


def test_daytona_multilane_runner_is_opus5_and_lane_scoped() -> None:
    campaign = (OPS / "daytona-campaign.sh").read_text(encoding="utf-8")
    lane = (OPS / "daytona-lane.sh").read_text(encoding="utf-8")
    assert "CG_DAYTONA_JOB_NAME" in campaign
    assert "model arm drifted" in campaign
    assert '"$JOB_NAME"' in campaign
    assert "cybergym-opus5-cyber-pass-" in lane
    assert 'while [ "$pass_index" -le 3 ]' in lane
    assert "daytona-finalize" in lane
    assert "CG_DAYTONA_PLAN_DIR" in lane
    assert "CG_DAYTONA_TASK_FILE" in lane
    assert "CG_RESULTS_DIR" in lane
    assert "ANTHROPIC_API_KEY" not in lane


def test_existing_host_key_update_preserves_internal_server_and_runtime_settings() -> None:
    update = (OPS / "update-secrets.sh").read_text(encoding="utf-8")
    assert "HUD API key" in update
    assert "Anthropic API key" in update
    assert "Daytona API key" in update
    assert 'server = read_private(paths["server"])' in update
    assert 'server.get("CYBERGYM_API_KEY")' in update
    assert 'write_atomic(paths["server"]' not in update
    assert 'runner["HUD_API_KEY"] = hud_key' in update
    assert 'runner["ANTHROPIC_API_KEY"] = anthropic_key' in update
    assert 'daytona["DAYTONA_API_KEY"] = daytona_key' in update


def test_daytona_ready_uses_current_gated_24x8_topology() -> None:
    ready = (OPS / "daytona-ready.sh").read_text(encoding="utf-8")
    dispatcher = (OPS / "cybergym-ops").read_text(encoding="utf-8")
    assert "Usage: daytona-ready.sh" in ready
    assert "--lanes 24 --max-concurrent 8" in ready
    assert "campaign-preflight.sh" in ready
    assert "daytona-preflight.sh" in ready
    assert "daytona-ready" in dispatcher


def test_daytona_control_is_boundary_safe_and_resume_is_spend_gated() -> None:
    control = (OPS / "daytona-control.sh").read_text(encoding="utf-8")
    assert "Pause requested" in control
    assert "active shard, if any, will finish" in control
    assert "resume requires --confirm-paid-selection" in control
    assert 'daytona-lane.sh" --lane' in control
    assert "sed 's/^0*//'" in control


def test_campaign_service_uses_a_deterministic_operator_path() -> None:
    installer = (OPS / "install-campaign-service.sh").read_text(encoding="utf-8")
    assert "OPERATOR_PATH=/home/$OPERATOR/.local/bin:/usr/local/bin:/usr/bin:/bin" in installer
    assert "POETRY_CACHE_DIR=/srv/cybergym-runtime/cache/poetry" in installer
    assert "Environment=HOME=/home/$OPERATOR" in installer
    assert "Environment=PATH=$OPERATOR_PATH" in installer
    assert "Environment=POETRY_CACHE_DIR=$POETRY_CACHE_DIR" in installer
    assert "campaign-preflight --max-concurrent" not in installer
    assert "campaign.sh` performs the authoritative no-spend preflight" in installer


def test_setup_and_services_do_not_mutate_the_pinned_checkout() -> None:
    setup = (OPS / "setup.sh").read_text(encoding="utf-8")
    server = (OPS / "server.sh").read_text(encoding="utf-8")
    server_installer = (OPS / "install-service.sh").read_text(encoding="utf-8")
    assert 'uv sync --frozen --project "$REPOSITORY_ROOT/integrations/hud"' in setup
    assert 'uv run --frozen --no-sync --project "$REPOSITORY_ROOT/integrations/hud"' in setup
    assert '"$CG_UV_BIN" run --frozen --no-sync --project' in server
    assert "ReadWritePaths=/srv/cybergym " not in server_installer
    assert "ReadWritePaths=/srv/cybergym/results-og-fidelity" in server_installer


def test_smoke_is_exactly_one_task_and_one_slot() -> None:
    text = (OPS / "smoke.sh").read_text(encoding="utf-8")
    assert "--all" not in text
    assert "--first-n" not in text
    assert "--max-concurrent 1" in text
    assert "--max-iter 10" in text
    assert "--max-output-tokens 2048" in text
    assert '--job-name "$JOB_NAME"' in text
    assert "--reasoning-effort" not in text
    assert "cybergym-claude-opus-5-no-internet-v1" in text
    assert '--runtime-network "$RUNTIME_NETWORK"' in text


def test_committed_env_template_has_names_but_no_secret_values() -> None:
    lines = (OPS / "env.example").read_text(encoding="utf-8").splitlines()
    assignments = {
        name: value for line in lines if line and not line.startswith("#") for name, value in [line.split("=", 1)]
    }
    for secret in (
        "HUD_API_KEY",
        "CYBERGYM_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "LLM_API_KEY",
    ):
        assert secret in assignments
        assert assignments[secret] == ""
    assert assignments["CG_SMOKE_TASK_ID"] == "arvo:10400"
    assert assignments["CG_MODEL"] == "claude-opus-5"
    assert assignments["CG_REASONING_EFFORT"] == ""
    assert assignments["CG_JOB_NAME"] == "cybergym-claude-opus-5-no-internet-v1"
    assert assignments["CG_RUNTIME_NETWORK"] == "cybergym-no-internet"
    assert assignments["CG_SERVER_URL"] == "http://172.30.0.1:8666"
    assert assignments["CG_SERVER_MODE"] == "images"
    assert assignments["CG_SERVER_BINARY_DIR"] == ""


def test_integration_installs_the_upstream_server_extra() -> None:
    text = (ROOT / "integrations" / "hud" / "pyproject.toml").read_text(encoding="utf-8")
    assert '"cybergym[server]"' in text


def test_secret_entry_and_dispatch_never_put_values_in_argv() -> None:
    configure = (OPS / "configure-secrets.sh").read_text(encoding="utf-8")
    dispatcher = (OPS / "cybergym-ops").read_text(encoding="utf-8")
    assert "getpass.getpass" in configure
    assert "/dev/tty" in configure
    assert 'open(tty_path, "r+"' not in configure
    assert "stream=tty" not in configure
    assert "secrets.token_urlsafe" in configure
    assert "/etc/cybergym/server.env" in configure
    assert "/etc/cybergym/runner.env" in configure
    assert "/etc/cybergym/daytona.env" in configure
    assert "/etc/cybergym/relay.env" in configure
    assert "secrets.token_hex(32)" in configure
    assert '"CG_DATA_DIR": "/srv/cybergym-runtime/task-data/cybergym-data/data"' in configure
    assert '"CG_DATA_PROVENANCE": "/srv/cybergym-runtime/task-data/provenance/PROVENANCE.json"' in configure
    assert '"CG_SERVER_DEPLOYMENT_SEAL": "/etc/cybergym/server-attestation.json"' in configure
    assert '"CG_CAMPAIGN_MAX_CONCURRENT": "4"' in configure
    assert "HUD_API_KEY=" not in configure
    assert "ANTHROPIC_API_KEY=" not in configure
    assert "try-restart" not in configure
    assert "set +x" in dispatcher
    assert 'exec "$SCRIPT_DIR/smoke.sh"' in dispatcher
    update = (OPS / "update-secrets.sh").read_text(encoding="utf-8")
    assert "--anthropic-only" in update
    assert 'anthropic_only = os.sys.argv[2] == "1"' in update
    assert 'exec sudo env \\' in update
    assert '"$SCRIPT_DIR/update-secrets.sh" "$@"' in update


def test_fresh_host_assets_are_exactly_pinned() -> None:
    artifacts = ROOT / "integrations" / "hud" / "artifacts" / "cybergym-source"
    provenance_path = artifacts / "PROVENANCE.json"
    manifest_path = artifacts / "selected-manifest.json"
    assert hashlib.sha256(provenance_path.read_bytes()).hexdigest() == (
        "9246b82aa98f2f1afcede95f9045fae4429a8da7289966bad2c728af70f48cb5"
    )
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == (
        "62020973579feafe340c756dd8e3aa0dc7d0e1e8b39674bd4063baa42c5a97ea"
    )
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert provenance["revision"] == "bde190ded494e52bc684b66073b436c9d992c7c6"
    assert provenance["file_count"] == 3_017
    assert provenance["total_bytes"] == 118_156_327_554
    assert len(manifest["files"]) == 3_017
    assert sum(row["size"] for row in manifest["files"]) == 118_156_327_554


def test_fresh_host_installer_is_resumable_and_verifies_public_artifacts() -> None:
    corpus = (OPS / "install-corpus.py").read_text(encoding="utf-8")
    bootstrap = (OPS / "bootstrap-host.sh").read_text(encoding="utf-8")
    assert "--continue-at" in corpus
    assert "BINARY_ARCHIVE_SHA256" in corpus
    assert "SOURCE_MANIFEST_SHA256" in corpus
    assert "SOURCE_PROVENANCE_SHA256" in corpus
    assert "os.replace(partial, destination)" in corpus
    assert "ThreadPoolExecutor" in corpus
    assert "_normalize_public_source_permissions()" in corpus
    assert "os.chmod(path, 0o755 if path.is_dir() else (0o555 if mode & 0o111 else 0o444))" in corpus
    assert "install-corpus.py" in bootstrap
    assert "cybergym-hud-attest-grader capture" in bootstrap
    assert "daytona-ready" in bootstrap
    assert "Protected key configuration already exists" in bootstrap
    assert 'LD_LIBRARY_PATH="${LD_LIBRARY_PATH-}"' in bootstrap
    assert 'runuser -u "$OPERATOR" -- git -C "$REPOSITORY_ROOT" status' in bootstrap
    session = (OPS / "bootstrap-session.sh").read_text(encoding="utf-8")
    assert '"$SCRIPT_DIR/bootstrap-host.sh"' in session
    assert 'command="cd $quoted_root && nix run .#bootstrap"' not in session


def test_public_relay_exposes_only_task_scoped_https() -> None:
    relay = (OPS / "relay.sh").read_text(encoding="utf-8")
    installer = (OPS / "install-relay.sh").read_text(encoding="utf-8")
    dispatcher = (OPS / "cybergym-ops").read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in relay
    assert "--enable-admin" in relay
    assert "sslip.io" in installer
    assert "reverse_proxy 127.0.0.1:18765" in installer
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in installer
    assert "CG_DAYTONA_RELAY_URL" in installer
    assert "CG_DAYTONA_RELAY_CIDRS" in installer
    assert "CG_DAYTONA_GRADER_ADMIN_URL" in installer
    assert "tailscale funnel" not in installer.lower()
    assert "tailscale serve" not in installer.lower()
    assert '. "$RELAY_ENV"' in dispatcher


def test_daytona_fleet_controls_are_durable_and_boundary_safe() -> None:
    fleet = (OPS / "daytona-fleet.sh").read_text(encoding="utf-8")
    installer = (OPS / "install-daytona-fleet.sh").read_text(encoding="utf-8")
    assert "start|status|pause|resume" in fleet
    assert "daytona-control pause" in fleet
    assert 'daytona-control.py" clear' in fleet
    assert "systemctl start" in fleet
    assert "systemctl stop" not in fleet
    assert "Restart=on-failure" in installer
    assert "RestartSec=60" in installer
    assert "TimeoutStopSec=infinity" in installer
    assert "cybergym-daytona@.service" in installer
    assert "pass@3" in installer


def test_root_readme_is_a_three_key_fresh_ec2_handoff() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for expected in (
        "nix run .#bootstrap",
        "nix run .#bootstrap-session",
        "HUD_API_KEY",
        "ANTHROPIC_API_KEY",
        "DAYTONA_API_KEY",
        "nix run .#daytona -- start",
        "nix run .#daytona -- status",
        "nix run .#daytona -- pause",
        "nix run .#daytona -- resume",
        "final-hud-reported-4521.json",
        "final-pass-at-3.json",
    ):
        assert expected in readme
    assert "HUD's AWS account" not in readme


def test_pass3_finalizer_is_local_strict_and_exported() -> None:
    finalizer = (OPS / "daytona-finalize.sh").read_text(encoding="utf-8")
    dispatcher = (OPS / "cybergym-ops").read_text(encoding="utf-8")
    flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
    report = (ROOT / "integrations" / "hud" / "cybergym_hud" / "pass3_report.py").read_text(
        encoding="utf-8"
    )
    assert "cybergym-hud-finalize-pass3" in finalizer
    assert "daytona-finalize" in dispatcher
    assert "finalize-pass3" in flake
    assert "final-hud-reported-4521.json" in report
    assert "final-pass-at-3.json" in report
    assert "fetch_job_traces" not in report
    assert "PlatformClient" not in report


def test_nix_apps_export_the_compiler_runtime_for_binary_wheels() -> None:
    flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
    configure = (OPS / "configure-secrets.sh").read_text(encoding="utf-8")
    assert flake.count('export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath') == 2
    assert flake.count('export XDG_CACHE_HOME="\'\'${CYBERGYM_OPERATOR_CACHE:-/srv/cybergym/operator-cache}"') == 2
    assert "\n          gcc\n" in flake
    assert "LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath" in flake
    assert 'XDG_CACHE_HOME = "/srv/cybergym/operator-cache"' in flake
    assert configure.count('"LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH", "")') == 2


def test_pulled_opus_pass1_complement_is_exact() -> None:
    completed = (OPS / "opus5-pass1-completed-from-pull.txt").read_text().splitlines()
    missing = (OPS / "opus5-missing-pass1-tasks.txt").read_text().splitlines()
    assert len(completed) == len(set(completed)) == 89
    assert len(missing) == len(set(missing)) == 1418
    assert set(completed).isdisjoint(missing)
    flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
    assert "run-missing-pass1" in flake
    wrapper = (OPS / "run-missing-pass1.sh").read_text(encoding="utf-8")
    assert "--confirm-spend YES" in wrapper
    assert "opus5-missing-pass1-tasks.txt" in wrapper
    assert "opus5-pass3/private-inputs" in wrapper
    assert 'chmod 600 "$temporary_task_file"' in wrapper
    assert 'cmp -s "$TASK_FILE" "$temporary_task_file"' in wrapper
    assert "CG_DAYTONA_TASK_FILE=$private_task_file" in wrapper
    continuation = (OPS / "continue-pass3.sh").read_text(encoding="utf-8")
    assert "for pass_index in 2 3" in continuation
    assert "cybergym-opus5-cyber-pass-$pass_index" in continuation
    assert "run-missing-pass1.sh" not in continuation
    assert "continue-pass3" in flake


def test_daytona_provider_probe_preserves_the_built_openhands_cache() -> None:
    campaign = (ROOT / "integrations" / "hud" / "cybergym_hud" / "daytona_campaign.py").read_text(
        encoding="utf-8"
    )
    assert '"XDG_CACHE_HOME",' in campaign
    assert '"LD_LIBRARY_PATH",' in campaign


def test_private_server_is_unmasked_and_internal_network_only() -> None:
    server = (OPS / "server.sh").read_text(encoding="utf-8")
    installer = (OPS / "install-service.sh").read_text(encoding="utf-8")
    assert 'docker network inspect "$CG_RUNTIME_NETWORK"' in server
    assert "172.30.0.1" in server
    assert "cybergym-hud-runtime-network verify" in server
    assert "public default is forbidden" in server
    assert "--mask_map_path" in server  # only in the explanatory no-mask comment
    assert 'set -- "$@" --binary_dir' in server
    assert "EnvironmentFile=/etc/cybergym/server.env" in installer
    assert "systemctl enable cybergym-server.service" in installer
    assert "systemctl restart cybergym-server.service" in installer
    assert "systemctl is-active --quiet cybergym-server.service" in installer
    assert "LimitCORE=0" in installer


def test_campaign_service_is_preflighted_durable_and_retries_exact_error_rows() -> None:
    installer = (OPS / "install-campaign-service.sh").read_text(encoding="utf-8")
    assert "campaign-preflight --max-concurrent 4" not in installer
    assert "campaign.sh` performs the authoritative no-spend preflight" in installer
    assert "--confirm-paid-all --continue-after-errors --max-concurrent 4 --shard-size 12" in installer
    assert "Restart=on-failure" in installer
    assert "StartLimitIntervalSec=0" in installer
    assert "cybergym-server.service" in installer
    assert "LimitCORE=0" in installer
    assert "systemctl enable cybergym-campaign.service" in installer
    assert "systemctl start cybergym-campaign.service" in installer


def test_readme_covers_operator_handoff_and_spend_guards() -> None:
    text = (ROOT / "integrations" / "hud" / "README.md").read_text(encoding="utf-8")
    for expected in (
        "ops/setup.sh",
        "ops/cybergym-ops preflight",
        "ops/cybergym-ops smoke --confirm-spend",
        "ops/configure-secrets.sh",
        "ops/install-service.sh",
        "HUD_API_KEY",
        "filetracking/1",
        "--all --confirm-paid-all",
        "https://www.hud.ai/jobs/JOB_ID",
        "https://www.hud.ai/trace/TRACE_ID",
        "## Artifacts, file tracking, and cleanup",
    ):
        assert expected in text

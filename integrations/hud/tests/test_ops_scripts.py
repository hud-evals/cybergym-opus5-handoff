from __future__ import annotations

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
        "install-service.sh",
        "install-campaign-service.sh",
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
    assert "https://api.openai.com/v1/models/" in text
    assert "SERVER_MODE=${CG_SERVER_MODE:-images}" in text
    assert "cybergym/oss-fuzz-base-runner:latest" in text
    assert 'IFS= read -r RUNNER_IMAGE <"$BINARY_TASK/$mode/runner"' in text
    assert "git-lfs.github.com/spec/v1" in text
    assert 'tar -tzf "$TASK_DATA/repo-vul.tar.gz"' in text
    assert '"$SCRIPT_DIR/runtime-image.sh" verify' in text
    assert '"$SCRIPT_DIR/verify-reasoning-transport.py"' in text
    assert "CG_REASONING_EFFORT=xhigh" in text


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
        "gpt-5.6-sol",
        "xhigh",
        "cybergym-gpt5.6-sol",
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
    assert "OPENAI_API_KEY" not in campaign
    assert "HUD_API_KEY" not in campaign
    assert "CYBERGYM_API_KEY" not in campaign
    assert "cybergym-hud-preflight-catalog" in preflight
    assert "full-corpus-preflight.json" in preflight
    assert "campaign-preflight" in dispatcher
    assert 'exec "$SCRIPT_DIR/campaign.sh"' in dispatcher


def test_campaign_service_uses_a_deterministic_operator_path() -> None:
    installer = (OPS / "install-campaign-service.sh").read_text(encoding="utf-8")
    assert "OPERATOR_PATH=/home/$OPERATOR/.local/bin:/usr/local/bin:/usr/bin:/bin" in installer
    assert "POETRY_CACHE_DIR=/srv/cybergym-runtime/cache/poetry" in installer
    assert 'env HOME="/home/$OPERATOR" PATH="$OPERATOR_PATH" POETRY_CACHE_DIR="$POETRY_CACHE_DIR"' in installer
    assert "Environment=HOME=/home/$OPERATOR" in installer
    assert "Environment=PATH=$OPERATOR_PATH" in installer
    assert "Environment=POETRY_CACHE_DIR=$POETRY_CACHE_DIR" in installer


def test_smoke_is_exactly_one_task_and_one_slot() -> None:
    text = (OPS / "smoke.sh").read_text(encoding="utf-8")
    assert "--all" not in text
    assert "--first-n" not in text
    assert "--max-concurrent 1" in text
    assert "--max-iter 10" in text
    assert '--job-name "$JOB_NAME"' in text
    assert '--reasoning-effort "$REASONING_EFFORT"' in text
    assert "cybergym-gpt5.6-sol" in text


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
    assert assignments["CG_MODEL"] == "gpt-5.6-sol"
    assert assignments["CG_REASONING_EFFORT"] == "xhigh"
    assert assignments["CG_JOB_NAME"] == "cybergym-gpt5.6-sol"
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
    assert '"CG_DATA_DIR": "/srv/cybergym-runtime/task-data/cybergym-data/data"' in configure
    assert '"CG_DATA_PROVENANCE": "/srv/cybergym-runtime/task-data/provenance/PROVENANCE.json"' in configure
    assert '"CG_SERVER_DEPLOYMENT_SEAL": "/etc/cybergym/server-attestation.json"' in configure
    assert '"CG_CAMPAIGN_MAX_CONCURRENT": "4"' in configure
    assert "HUD_API_KEY=" not in configure
    assert "OPENAI_API_KEY=" not in configure
    assert "try-restart" not in configure
    assert "set +x" in dispatcher
    assert 'exec "$SCRIPT_DIR/smoke.sh"' in dispatcher


def test_private_server_is_unmasked_and_docker_bridge_only() -> None:
    server = (OPS / "server.sh").read_text(encoding="utf-8")
    installer = (OPS / "install-service.sh").read_text(encoding="utf-8")
    assert "docker network inspect bridge" in server
    assert "public default is forbidden" in server
    assert "--mask_map_path" in server  # only in the explanatory no-mask comment
    assert 'set -- "$@" --binary_dir' in server
    assert "EnvironmentFile=/etc/cybergym/server.env" in installer
    assert "systemctl enable cybergym-server.service" in installer
    assert "systemctl restart cybergym-server.service" in installer
    assert "systemctl is-active --quiet cybergym-server.service" in installer
    assert "LimitCORE=0" in installer


def test_campaign_service_is_preflighted_durable_and_does_not_error_loop() -> None:
    installer = (OPS / "install-campaign-service.sh").read_text(encoding="utf-8")
    assert "campaign-preflight --max-concurrent 4" in installer
    assert "--confirm-paid-all --max-concurrent 4 --shard-size 12" in installer
    assert "Restart=on-abnormal" in installer
    assert "Restart=on-failure" not in installer
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

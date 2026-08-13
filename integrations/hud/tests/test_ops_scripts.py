from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OPS = ROOT / "integrations" / "hud" / "ops"
SCRIPTS = tuple(OPS / name for name in ("setup.sh", "preflight.sh", "smoke.sh"))


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


def test_smoke_refuses_spend_before_preflight_or_uv() -> None:
    result = _run(str(OPS / "smoke.sh"), env={"PATH": os.environ["PATH"]})
    assert result.returncode != 0
    assert "--confirm-spend" in result.stderr
    assert "preflight" not in result.stdout


def test_smoke_is_exactly_one_task_and_one_slot() -> None:
    text = (OPS / "smoke.sh").read_text(encoding="utf-8")
    assert "--all" not in text
    assert "--first-n" not in text
    assert "--max-concurrent 1" in text
    assert "--max-iter 10" in text


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


def test_readme_covers_operator_handoff_and_spend_guards() -> None:
    text = (ROOT / "integrations" / "hud" / "README.md").read_text(encoding="utf-8")
    for expected in (
        "ops/setup.sh",
        "ops/preflight.sh",
        "ops/smoke.sh --confirm-spend",
        "HUD_API_KEY",
        "filetracking/1",
        "--all --confirm-paid-all",
        "https://www.hud.ai/jobs/JOB_ID",
        "https://www.hud.ai/trace/TRACE_ID",
        "## Artifacts, file tracking, and cleanup",
    ):
        assert expected in text

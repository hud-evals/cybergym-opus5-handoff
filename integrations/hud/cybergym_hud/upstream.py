"""Checks and exec-only entry points for the pinned upstream scripts."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from .contract import CONTRACT, PINNED_AGENT_COMMIT, repository_root, validate_contract


def _git(path: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"could not inspect upstream agent checkout: git {' '.join(args)}") from exc
    return result.stdout.strip()


def require_upstream_agent_checkout(root: str | Path | None = None) -> Path:
    """Return the exact clean pinned agent checkout or fail with an actionable error."""

    checkout = repository_root(root)
    agents = checkout / str(CONTRACT["agent_scaffold"]["gitlink_path"])
    script = agents / "openhands/run.py"
    if not script.is_file():
        raise RuntimeError("initialize the pinned agent with `git submodule update --init --recursive examples/agents`")
    if _git(agents, "rev-parse", "HEAD") != PINNED_AGENT_COMMIT:
        raise RuntimeError(f"examples/agents must be at {PINNED_AGENT_COMMIT}")
    dirty = _git(agents, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        raise RuntimeError("examples/agents has local changes; exact native mode requires a clean checkout")
    prompt = agents / str(CONTRACT["agent_scaffold"]["prompt_source"])
    digest = hashlib.sha256(prompt.read_bytes()).hexdigest()
    if digest != CONTRACT["agent_scaffold"]["prompt_sha256"]:
        raise RuntimeError(f"upstream OpenHands prompt digest is {digest}, expected the pinned bytes")
    return agents


def _exec_python(script: Path, argv: list[str]) -> None:
    os.execv(sys.executable, [sys.executable, str(script), *argv])


def run_openhands() -> None:
    """Replace this process with the exact pinned upstream OpenHands CLI."""

    root = repository_root()
    validate_contract(root=root)
    agents = require_upstream_agent_checkout(root)
    _exec_python(agents / "openhands/run.py", sys.argv[1:])


def run_verifier() -> None:
    """Replace this process with CyberGym's exact upstream verification CLI."""

    root = repository_root()
    validate_contract(root=root)
    _exec_python(root / "scripts/verify_agent_result.py", sys.argv[1:])


__all__ = ["require_upstream_agent_checkout", "run_openhands", "run_verifier"]

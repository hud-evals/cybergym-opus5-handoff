from __future__ import annotations

import sys
from pathlib import Path

from cybergym_hud import upstream


def test_openhands_passthrough_preserves_all_arguments(monkeypatch, tmp_path: Path) -> None:
    agents = tmp_path / "examples/agents"
    script = agents / "openhands/run.py"
    script.parent.mkdir(parents=True)
    script.touch()
    seen = {}

    monkeypatch.setattr(upstream, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(upstream, "validate_contract", lambda **_kwargs: None)
    monkeypatch.setattr(upstream, "require_upstream_agent_checkout", lambda _root: agents)
    monkeypatch.setattr(upstream, "_exec_python", lambda path, argv: seen.update(path=path, argv=argv))
    monkeypatch.setattr(sys, "argv", ["command", "--model", "gpt-test", "--timeout", "7"])

    upstream.run_openhands()
    assert seen == {"path": script, "argv": ["--model", "gpt-test", "--timeout", "7"]}


def test_verifier_passthrough_preserves_all_arguments(monkeypatch, tmp_path: Path) -> None:
    script = tmp_path / "scripts/verify_agent_result.py"
    script.parent.mkdir(parents=True)
    script.touch()
    seen = {}

    monkeypatch.setattr(upstream, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(upstream, "validate_contract", lambda **_kwargs: None)
    monkeypatch.setattr(upstream, "_exec_python", lambda path, argv: seen.update(path=path, argv=argv))
    monkeypatch.setattr(sys, "argv", ["command", "--agent_id", "abc"])

    upstream.run_verifier()
    assert seen == {"path": script, "argv": ["--agent_id", "abc"]}

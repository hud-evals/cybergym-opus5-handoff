from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cybergym_hud.cleanup import OPENHANDS_RUNTIME_IMAGE, cleanup_tracked_root


def test_cleanup_uses_narrow_docker_fallback_for_root_owned_children(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "hud-rollout-00000000000000000000000000000001"
    child = root / "arvo_1-agent" / "workspace"
    child.mkdir(parents=True)
    (child / "poc").write_text("proof", encoding="utf-8")

    real_rmtree = shutil.rmtree
    rmtree_calls = 0

    def blocked_once(path, *, ignore_errors):
        nonlocal rmtree_calls
        rmtree_calls += 1
        if rmtree_calls == 1:
            return
        real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr("cybergym_hud.cleanup.shutil.rmtree", blocked_once)
    container_calls = []

    class Containers:
        def run(self, image, **kwargs):
            container_calls.append((image, kwargs))
            for item in tuple(root.iterdir()):
                real_rmtree(item)

    class Client:
        containers = Containers()
        closed = False

        def close(self):
            self.closed = True

    client = Client()
    cleanup_tracked_root(root, docker_client_factory=lambda: client)

    assert not root.exists()
    assert client.closed is True
    assert container_calls[0][0] == OPENHANDS_RUNTIME_IMAGE
    kwargs = container_calls[0][1]
    assert kwargs["volumes"] == {str(root.resolve()): {"bind": "/cleanup", "mode": "rw"}}
    assert kwargs["network_disabled"] is True
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["cap_add"] == ["DAC_OVERRIDE"]
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["remove"] is True


def test_cleanup_refuses_non_rollout_paths(tmp_path: Path) -> None:
    path = tmp_path / "workspace"
    path.mkdir()
    with pytest.raises(RuntimeError, match="refusing cleanup"):
        cleanup_tracked_root(path, docker_client_factory=lambda: pytest.fail("must not use Docker"))

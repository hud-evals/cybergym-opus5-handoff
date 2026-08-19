from __future__ import annotations

import errno
from pathlib import Path

import pytest

from cybergym_hud.artifact_storage import enforce_private_file_mode, has_private_storage
from cybergym_hud.scheduler import write_summary


def test_daytona_volume_tolerates_only_eperm_inside_explicit_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    volume = tmp_path / "artifacts"
    volume.mkdir()
    inside = volume / "ledger.jsonl"
    outside = tmp_path / "outside.json"
    monkeypatch.setenv("CG_DAYTONA_ARTIFACT_VOLUME_ROOT", str(volume))

    def denied(_descriptor: int, _mode: int) -> None:
        raise PermissionError(errno.EPERM, "volume controls permissions")

    monkeypatch.setattr("cybergym_hud.artifact_storage.os.fchmod", denied)
    enforce_private_file_mode(123, inside)
    assert has_private_storage(inside, 0o666) is True
    with pytest.raises(PermissionError):
        enforce_private_file_mode(123, outside)
    assert has_private_storage(outside, 0o666) is False


def test_summary_writes_directly_inside_daytona_volume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    volume = tmp_path / "artifacts"
    volume.mkdir()
    monkeypatch.setenv("CG_DAYTONA_ARTIFACT_VOLUME_ROOT", str(volume))
    path = volume / "summary.json"

    write_summary(path, {"ok": True})

    assert path.read_text() == '{\n  "ok": true\n}\n'
    assert list(volume.iterdir()) == [path]

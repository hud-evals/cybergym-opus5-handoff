from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from cybergym_hud.catalog_preflight import (
    CatalogPreflightError,
    _tree_digest,
    _validate_binary_tree_symlinks,
    validate_full_catalog,
)


def _write_task(data_dir: Path, task_id: str) -> None:
    subset, subid = task_id.split(":")
    root = data_dir / subset / subid
    root.mkdir(parents=True)
    (root / "description.txt").write_text("test task\n", encoding="utf-8")
    with tarfile.open(root / "repo-vul.tar.gz", "w:gz") as archive:
        payload = b"source"
        info = tarfile.TarInfo("source.c")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))


def test_full_catalog_preflight_validates_every_source_and_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ids = ("arvo:1", "oss-fuzz:2")
    monkeypatch.setattr("cybergym_hud.catalog_preflight.task_ids", lambda _root: ids)
    for task_id in ids:
        _write_task(tmp_path / "data", task_id)
    expected = {
        "n132/arvo:1-vul",
        "n132/arvo:1-fix",
        "cybergym/oss-fuzz:2-vul",
        "cybergym/oss-fuzz:2-fix",
    }
    observed: set[str] = set()

    def image_identity(ref: str) -> str | None:
        observed.add(ref)
        return f"sha256:{ref}" if ref in expected else None

    report = validate_full_catalog(
        repository_root=tmp_path,
        data_dir=tmp_path / "data",
        server_mode="images",
        server_binary_dir=None,
        max_concurrent=2,
        image_identity=image_identity,
        cpu_count=8,
        memory_bytes=18 * 1024**3,
    )
    assert report["no_model_call"] is True
    assert report["task_count"] == 2
    assert report["validated_tar_count"] == 2
    assert observed == expected


def test_full_catalog_preflight_rejects_missing_corpus_before_spend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cybergym_hud.catalog_preflight.task_ids", lambda _root: ("arvo:1",))
    with pytest.raises(CatalogPreflightError, match="missing description"):
        validate_full_catalog(
            repository_root=tmp_path,
            data_dir=tmp_path / "data",
            server_mode="images",
            server_binary_dir=None,
            max_concurrent=1,
            image_identity=lambda _ref: None,
            cpu_count=4,
            memory_bytes=10 * 1024**3,
        )


def test_capacity_gate_rejects_two_rollouts_on_a_16_gib_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cybergym_hud.catalog_preflight.task_ids", lambda _root: ())
    with pytest.raises(CatalogPreflightError, match="undersized"):
        validate_full_catalog(
            repository_root=tmp_path,
            data_dir=tmp_path,
            server_mode="images",
            server_binary_dir=None,
            max_concurrent=2,
            image_identity=lambda _ref: "sha256:test",
            cpu_count=8,
            memory_bytes=16 * 1024**3,
        )


def test_binary_symlink_validation_accepts_contained_relative_links(tmp_path: Path) -> None:
    root = tmp_path / "grader"
    target = root / "arvo" / "1" / "vul" / "out" / "lib" / "libexample.so.1"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"library")
    link = target.with_name("libexample.so")
    link.symlink_to(target.name)

    counts, errors = _validate_binary_tree_symlinks(root)

    assert errors == []
    assert counts == {"total": 1, "relative": 1, "reviewed_absolute": 0}


def test_binary_symlink_validation_rejects_broken_relative_links(tmp_path: Path) -> None:
    root = tmp_path / "grader"
    link = root / "arvo" / "1" / "vul" / "out" / "libexample.so"
    link.parent.mkdir(parents=True)
    link.symlink_to("missing.so")

    counts, errors = _validate_binary_tree_symlinks(root)

    assert counts == {"total": 1, "relative": 0, "reviewed_absolute": 0}
    assert errors == ["broken relative binary grader symlink: arvo/1/vul/out/libexample.so -> missing.so"]


def test_binary_symlink_validation_rejects_relative_escape(tmp_path: Path) -> None:
    root = tmp_path / "grader"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    link = root / "escape"
    link.symlink_to("../outside")

    counts, errors = _validate_binary_tree_symlinks(root)

    assert counts == {"total": 1, "relative": 0, "reviewed_absolute": 0}
    assert errors == ["escaping relative binary grader symlink: escape -> ../outside"]


def test_binary_symlink_validation_accepts_only_reviewed_absolute_link(tmp_path: Path) -> None:
    root = tmp_path / "grader"
    reviewed = root / "arvo" / "60121" / "fix" / "out" / "oss-fuzz-zeek-scripts" / "tests"
    reviewed.parent.mkdir(parents=True)
    reviewed.symlink_to("/src/zeek/build/install-root/share/btest/data")

    counts, errors = _validate_binary_tree_symlinks(root)

    assert errors == []
    assert counts == {"total": 1, "relative": 0, "reviewed_absolute": 1}

    reviewed.unlink()
    reviewed.symlink_to("/src/unreviewed")
    counts, errors = _validate_binary_tree_symlinks(root)
    assert counts == {"total": 1, "relative": 0, "reviewed_absolute": 0}
    assert errors == [
        "unsupported absolute binary grader symlink: arvo/60121/fix/out/oss-fuzz-zeek-scripts/tests -> /src/unreviewed"
    ]

    reviewed.unlink()
    unreviewed = root / "arvo" / "60121" / "vul" / "out" / "oss-fuzz-zeek-scripts" / "tests"
    unreviewed.parent.mkdir(parents=True)
    unreviewed.symlink_to("/src/zeek/build/install-root/share/btest/data")
    counts, errors = _validate_binary_tree_symlinks(root)
    assert counts == {"total": 1, "relative": 0, "reviewed_absolute": 0}
    assert errors == [
        "unsupported absolute binary grader symlink: "
        "arvo/60121/vul/out/oss-fuzz-zeek-scripts/tests -> /src/zeek/build/install-root/share/btest/data"
    ]


def test_binary_tree_fingerprint_includes_symlink_target_metadata(tmp_path: Path) -> None:
    root = tmp_path / "grader"
    root.mkdir()
    (root / "first").write_bytes(b"same bytes")
    (root / "second").write_bytes(b"same bytes")
    link = root / "selected"
    link.symlink_to("first")
    first_digest = _tree_digest(root)

    link.unlink()
    link.symlink_to("second")

    assert _tree_digest(root) != first_digest

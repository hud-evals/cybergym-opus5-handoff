from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from cybergym_hud.catalog_preflight import (
    _REVIEWED_CONTAINER_ABSOLUTE_SYMLINKS,
    REVIEWED_BINARY_RUNNER_IDENTITIES,
    CatalogPreflightError,
    _attest_live_server,
    _binary_runner_identity_errors,
    _load_source_provenance,
    _require_protected_binary_tree,
    _require_protected_source_tree,
    _require_reviewed_binary_tree,
    _require_root_controlled_ancestors,
    _stable_server_attestation,
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


def _server_attestation() -> dict[str, object]:
    return {"service": "test", "server_mode": "test", "mask_map": False}


def test_server_attestation_fingerprint_excludes_only_observation_time() -> None:
    stable = {"repository_revision": "a" * 40, "binary_dir": "/grader", "port": 8666}
    first = {**stable, "attested_at": "first", "invocation_id": "one", "listener_inode": 41}
    second = {**stable, "attested_at": "second", "invocation_id": "two", "listener_inode": 42}
    assert _stable_server_attestation(first) == _stable_server_attestation(second)


def test_binary_runner_images_are_exact_linux_amd64_snapshots() -> None:
    identities = {
        reference: json.dumps(
            {
                "id": expected["id"],
                "repo_digests": [expected["repo_digest"]],
                "os": "linux",
                "architecture": "amd64",
            }
        )
        for reference, expected in REVIEWED_BINARY_RUNNER_IDENTITIES.items()
    }
    assert _binary_runner_identity_errors(identities) == []
    identities["cybergym/oss-fuzz-base-runner:latest"] = json.dumps(
        {"id": "sha256:drift", "repo_digests": [], "os": "linux", "architecture": "amd64"}
    )
    assert _binary_runner_identity_errors(identities) == [
        "binary grader runner image identity drifted: cybergym/oss-fuzz-base-runner:latest"
    ]


def test_source_provenance_is_pinned_and_manifest_hash_verified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(os, "listxattr", lambda *_args, **_kwargs: [], raising=False)
    data_root = tmp_path / "cybergym-data"
    data_dir = data_root / "data"
    provenance_dir = tmp_path / "provenance"
    provenance_dir.mkdir()
    files = [{"path": name, "lfs": None} for name in (".gitattributes", "README.md", "tasks.json")]
    for index in range(1_507):
        for name in ("description.txt", "repo-vul.tar.gz"):
            files.append(
                {
                    "path": f"data/arvo/{index}/{name}",
                    "lfs": {"sha256": f"{index:064x}"[-64:], "size": 1, "pointer_size": 1},
                }
            )
    manifest = {
        "repository": "sunblaze-ucb/cybergym",
        "repository_type": "dataset",
        "revision": "bde190ded494e52bc684b66073b436c9d992c7c6",
        "files": files,
    }
    manifest_path = provenance_dir / "selected-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o440)
    provenance = {
        "status": "verified",
        "repository": "sunblaze-ucb/cybergym",
        "repository_type": "dataset",
        "revision": "bde190ded494e52bc684b66073b436c9d992c7c6",
        "root": str(data_root.resolve()),
        "file_count": 3_017,
        "total_bytes": 118_156_327_554,
        "task_catalog": {"count": 1_507, "unique_count": 1_507},
        "git_objects_verified": 3,
        "lfs_xet_files_verified": 3_014,
        "gzip_tar_archives_verified": 1_507,
        "pointer_files_found": 0,
        "selected_manifest": str(manifest_path),
        "selected_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    provenance_path = provenance_dir / "PROVENANCE.json"
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    provenance_path.chmod(0o440)
    monkeypatch.setattr(
        "cybergym_hud.catalog_preflight.SOURCE_SELECTED_MANIFEST_SHA256",
        hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        "cybergym_hud.catalog_preflight.SOURCE_PROVENANCE_SHA256",
        hashlib.sha256(provenance_path.read_bytes()).hexdigest(),
    )

    hashes, fields = _load_source_provenance(provenance_path, data_dir=data_dir, require_root_owner=False)
    assert len(hashes) == 3_014
    assert fields["source_revision"] == provenance["revision"]

    real_listxattr = os.listxattr
    monkeypatch.setattr(
        os,
        "listxattr",
        lambda path, **kwargs: (
            ["system.posix_acl_access"] if Path(path) == manifest_path else real_listxattr(path, **kwargs)
        ),
    )
    with pytest.raises(CatalogPreflightError, match="malformed"):
        _load_source_provenance(provenance_path, data_dir=data_dir, require_root_owner=False)
    monkeypatch.setattr(os, "listxattr", real_listxattr)

    manifest_path.chmod(0o640)
    manifest_path.write_text("{}", encoding="utf-8")
    manifest_path.chmod(0o440)
    with pytest.raises(CatalogPreflightError, match="does not match"):
        _load_source_provenance(provenance_path, data_dir=data_dir, require_root_owner=False)


def test_source_provenance_rejects_replaceable_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provenance_path = tmp_path / "replaceable" / "PROVENANCE.json"
    provenance_path.parent.mkdir()
    provenance_path.write_text("{}", encoding="utf-8")

    def reject_ancestors(path: Path) -> None:
        assert path == provenance_path
        raise CatalogPreflightError("protected data ancestor is replaceable")

    monkeypatch.setattr(
        "cybergym_hud.catalog_preflight._require_root_controlled_ancestors",
        reject_ancestors,
    )
    with pytest.raises(CatalogPreflightError, match="ancestor is replaceable"):
        _load_source_provenance(provenance_path, data_dir=tmp_path / "data")


def test_source_provenance_rejects_file_access_acl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provenance_path = tmp_path / "PROVENANCE.json"
    provenance_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        os,
        "listxattr",
        lambda path, **_kwargs: ["system.posix_acl_access"] if Path(path) == provenance_path else [],
        raising=False,
    )
    with pytest.raises(CatalogPreflightError, match="non-symlink, non-writable"):
        _load_source_provenance(
            provenance_path,
            data_dir=tmp_path / "data",
            require_root_owner=False,
        )


def test_live_server_attestation_binds_mode_checkout_and_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    project = repository / "integrations/hud"
    project.mkdir(parents=True)
    binary = tmp_path / "binary"
    binary.mkdir()
    proc = tmp_path / "proc/123"
    proc.mkdir(parents=True)
    (proc / "cwd").symlink_to(repository, target_is_directory=True)
    command = [
        "/usr/local/bin/uv",
        "run",
        "--frozen",
        "--project",
        str(project),
        "python",
        "-m",
        "cybergym.server",
        "--host",
        "172.17.0.1",
        "--port",
        "8666",
        "--binary_dir",
        str(binary),
    ]
    (proc / "cmdline").write_bytes(b"\0".join(part.encode() for part in command) + b"\0")

    def run_command(command, **_kwargs):
        if "is-active" in command:
            return SimpleNamespace(returncode=0, stdout=b"")
        return SimpleNamespace(returncode=0, stdout="123\n")

    monkeypatch.setattr("cybergym_hud.catalog_preflight.load_deployment_seal", lambda _path: {"sealed": True})
    monkeypatch.setattr(
        "cybergym_hud.catalog_preflight.attest_live_binary_server",
        lambda **kwargs: {
            "binary_dir": str(kwargs["binary_dir"]),
            "host": kwargs["host"],
            "port": kwargs["port"],
            "mask_map": False,
        },
    )

    report = _attest_live_server(
        repository_root=repository,
        server_url="http://172.17.0.1:8666",
        server_mode="binary",
        server_binary_dir=binary,
        proc_root=tmp_path / "proc",
        run_command=run_command,
        deployment_seal=tmp_path / "seal.json",
    )
    assert report["binary_dir"] == str(binary)
    assert report["mask_map"] is False

    with pytest.raises(CatalogPreflightError, match="does not match"):
        _attest_live_server(
            repository_root=repository,
            server_url="http://172.17.0.1:8667",
            server_mode="binary",
            server_binary_dir=binary,
            proc_root=tmp_path / "proc",
            run_command=run_command,
            deployment_seal=tmp_path / "seal.json",
        )


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
        source_provenance=None,
        server_mode="images",
        server_binary_dir=None,
        max_concurrent=2,
        image_identity=image_identity,
        runtime_limit_probe=lambda: {
            "nano_cpus": 4_000_000_000,
            "memory": 8 * 1024**3,
            "memory_swap": 8 * 1024**3,
        },
        server_attestation=_server_attestation(),
        cpu_count=8,
        memory_bytes=22 * 1024**3,
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
            source_provenance=None,
            server_mode="images",
            server_binary_dir=None,
            max_concurrent=1,
            image_identity=lambda _ref: None,
            runtime_limit_probe=lambda: {
                "nano_cpus": 4_000_000_000,
                "memory": 8 * 1024**3,
                "memory_swap": 8 * 1024**3,
            },
            server_attestation=_server_attestation(),
            cpu_count=4,
            memory_bytes=14 * 1024**3,
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
            source_provenance=None,
            server_mode="images",
            server_binary_dir=None,
            max_concurrent=2,
            image_identity=lambda _ref: "sha256:test",
            runtime_limit_probe=lambda: {
                "nano_cpus": 4_000_000_000,
                "memory": 8 * 1024**3,
                "memory_swap": 8 * 1024**3,
            },
            server_attestation=_server_attestation(),
            cpu_count=8,
            memory_bytes=16 * 1024**3,
        )


def test_preflight_rejects_unenforced_runtime_limits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cybergym_hud.catalog_preflight.task_ids", lambda _root: ())
    with pytest.raises(CatalogPreflightError, match="did not preserve"):
        validate_full_catalog(
            repository_root=tmp_path,
            data_dir=tmp_path,
            source_provenance=None,
            server_mode="images",
            server_binary_dir=None,
            max_concurrent=1,
            image_identity=lambda _ref: "sha256:test",
            runtime_limit_probe=lambda: {"nano_cpus": 0, "memory": 0, "memory_swap": 0},
            server_attestation=_server_attestation(),
            cpu_count=4,
            memory_bytes=14 * 1024**3,
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


def test_full_binary_campaign_requires_reviewed_snapshot_digest() -> None:
    _require_reviewed_binary_tree("fe793d3ed06692b5566e3b1eeca91e39eabb87c5386dd7091d1c94516892b455")
    with pytest.raises(CatalogPreflightError, match="reviewed deployment snapshot"):
        _require_reviewed_binary_tree("0" * 64)


def test_full_binary_campaign_requires_root_owned_nonwritable_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "grader"
    root.mkdir()
    payload = root / "target"
    payload.write_bytes(b"grader")
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_uid=0, st_mode=0o100444))
    monkeypatch.setattr(os, "listxattr", lambda *_args, **_kwargs: [], raising=False)
    assert _require_protected_binary_tree(root) == 2

    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_uid=1000, st_mode=0o100644))
    with pytest.raises(CatalogPreflightError, match="not immutable|replaceable"):
        _require_protected_binary_tree(root)


def test_protected_root_rejects_replaceable_parent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = (tmp_path / "parent/data").resolve()
    root.mkdir(parents=True)
    monkeypatch.setattr(os, "listxattr", lambda *_args, **_kwargs: [], raising=False)
    original = Path.lstat

    def fake_lstat(path: Path):
        observed = original(path)
        uid = 1000 if path == root.parent else 0
        return SimpleNamespace(st_uid=uid, st_mode=observed.st_mode)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(CatalogPreflightError, match="replaceable"):
        _require_root_controlled_ancestors(root)


def test_source_campaign_requires_root_owned_nonwritable_regular_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    root.mkdir()
    (root / "task").write_bytes(b"source")
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_uid=0, st_mode=0o100440))
    monkeypatch.setattr(os, "listxattr", lambda *_args, **_kwargs: [], raising=False)
    assert _require_protected_source_tree(root) == 2

    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_uid=1000, st_mode=0o100440))
    with pytest.raises(CatalogPreflightError, match="not root-owned|replaceable"):
        _require_protected_source_tree(root)


def test_full_binary_catalog_requires_all_reviewed_absolute_links(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Short-circuit source validation with repeated well-formed IDs while
    # exercising the production cardinality-bound symlink completeness gate.
    ids = tuple(f"arvo:{index}" for index in range(1, 1_508))
    monkeypatch.setattr("cybergym_hud.catalog_preflight.task_ids", lambda _root: ids)
    grader = tmp_path / "grader"
    for relative, target in _REVIEWED_CONTAINER_ABSOLUTE_SYMLINKS.items():
        link = grader / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    missing = grader / next(iter(_REVIEWED_CONTAINER_ABSOLUTE_SYMLINKS))
    missing.unlink()

    with pytest.raises(CatalogPreflightError, match="missing one or more reviewed"):
        validate_full_catalog(
            repository_root=tmp_path,
            data_dir=tmp_path / "data",
            source_provenance=None,
            server_mode="binary",
            server_binary_dir=grader,
            max_concurrent=1,
            image_identity=lambda _ref: "sha256:test",
            runtime_limit_probe=_runtime_limits,
            server_attestation=_server_attestation(),
            cpu_count=4,
            memory_bytes=14 * 1024**3,
        )


def _runtime_limits() -> dict[str, int]:
    return {
        "nano_cpus": 4_000_000_000,
        "memory": 8 * 1024**3,
        "memory_swap": 8 * 1024**3,
    }


def test_binary_preflight_requires_arvo_libraries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cybergym_hud.catalog_preflight.task_ids", lambda _root: ("arvo:1",))
    _write_task(tmp_path / "data", "arvo:1")
    grader = tmp_path / "grader"
    for variant in ("vul", "fix"):
        root = grader / "arvo/1" / variant
        (root / "out").mkdir(parents=True)
        (root / "libs").mkdir()
        binary = root / "arvo"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
    (grader / "arvo/1/fix/libs").rmdir()

    with pytest.raises(CatalogPreflightError, match="missing binary library directory"):
        validate_full_catalog(
            repository_root=tmp_path,
            data_dir=tmp_path / "data",
            source_provenance=None,
            server_mode="binary",
            server_binary_dir=grader,
            max_concurrent=1,
            image_identity=lambda _ref: "sha256:test",
            runtime_limit_probe=_runtime_limits,
            server_attestation=_server_attestation(),
            cpu_count=4,
            memory_bytes=14 * 1024**3,
        )


def test_binary_preflight_requires_metadata_named_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cybergym_hud.catalog_preflight.task_ids", lambda _root: ("oss-fuzz:2",))
    _write_task(tmp_path / "data", "oss-fuzz:2")
    grader = tmp_path / "grader"
    for variant in ("vul", "fix"):
        root = grader / "oss-fuzz/2" / variant
        (root / "out").mkdir(parents=True)
        (root / "metadata.json").write_text('{"fuzz_target":"target"}', encoding="utf-8")
    missing = grader / "oss-fuzz/2/fix/out/target"
    present = grader / "oss-fuzz/2/vul/out/target"
    present.write_text("#!/bin/sh\n", encoding="utf-8")
    present.chmod(0o755)

    with pytest.raises(CatalogPreflightError, match=f"missing executable fuzz target: oss-fuzz:2/fix/{missing.name}"):
        validate_full_catalog(
            repository_root=tmp_path,
            data_dir=tmp_path / "data",
            source_provenance=None,
            server_mode="binary",
            server_binary_dir=grader,
            max_concurrent=1,
            image_identity=lambda _ref: "sha256:test",
            runtime_limit_probe=_runtime_limits,
            server_attestation=_server_attestation(),
            cpu_count=4,
            memory_bytes=14 * 1024**3,
        )

"""No-inference validation of every CyberGym task artifact and worker capacity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import docker

from .grader_attestation import (
    GraderAttestationError,
    attest_live_binary_server,
    load_deployment_seal,
    validate_binary_tree_immutability,
)
from .native import (
    CAMPAIGN_RUNTIME_MEMORY_BYTES,
    CAMPAIGN_RUNTIME_MEMORY_SWAP_BYTES,
    CAMPAIGN_RUNTIME_NANO_CPUS,
)
from .scheduler import write_summary
from .taskset import task_ids

CPU_PER_ROLLOUT = 4
MEMORY_PER_ROLLOUT_BYTES = 8 * 1024**3
HOST_MEMORY_RESERVE_BYTES = 6 * 1024**3
OPENHANDS_RUNTIME_IMAGE = "docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik"
SOURCE_REPOSITORY = "sunblaze-ucb/cybergym"
SOURCE_REVISION = "bde190ded494e52bc684b66073b436c9d992c7c6"
SOURCE_FILE_COUNT = 3_017
SOURCE_TOTAL_BYTES = 118_156_327_554
# The released binary-only grader corpus has no upstream revision manifest.
# This is therefore a reviewed deployment snapshot identity, not a claim about
# upstream provenance.  It was computed with _tree_digest/v1 over the complete
# 1,507-task VM corpus (92,888 entries; 130,195,047,160 apparent file bytes).
REVIEWED_BINARY_TREE_SHA256 = "fe793d3ed06692b5566e3b1eeca91e39eabb87c5386dd7091d1c94516892b455"
EXPECTED_RUNTIME_LIMITS = {
    "nano_cpus": CAMPAIGN_RUNTIME_NANO_CPUS,
    "memory": CAMPAIGN_RUNTIME_MEMORY_BYTES,
    "memory_swap": CAMPAIGN_RUNTIME_MEMORY_SWAP_BYTES,
}
REVIEWED_BINARY_RUNNER_IDENTITIES = {
    "cybergym/oss-fuzz-base-runner:20190802": {
        "id": "sha256:525c306351d7d45c5a1aac3de9a2481091f1d2b571caac3bf66458dc17c03822",
        "repo_digest": (
            "cybergym/oss-fuzz-base-runner@sha256:525c306351d7d45c5a1aac3de9a2481091f1d2b571caac3bf66458dc17c03822"  # noqa: E501
        ),
    },
    "cybergym/oss-fuzz-base-runner:20200102": {
        "id": "sha256:c880951087fb0ffef2a73ea64214701232adb97e5517f75edefee0a53353d7c6",
        "repo_digest": (
            "cybergym/oss-fuzz-base-runner@sha256:c880951087fb0ffef2a73ea64214701232adb97e5517f75edefee0a53353d7c6"  # noqa: E501
        ),
    },
    "cybergym/oss-fuzz-base-runner:20220102": {
        "id": "sha256:755b6ea506293e492e20cb011e2bec83470ea3e6159623707799c7c9f6b762c2",
        "repo_digest": (
            "cybergym/oss-fuzz-base-runner@sha256:755b6ea506293e492e20cb011e2bec83470ea3e6159623707799c7c9f6b762c2"  # noqa: E501
        ),
    },
    "cybergym/oss-fuzz-base-runner:latest": {
        "id": "sha256:05e940b156bc21267a13159ff0606909fb112f189b10ad1a60afe6f2dce6bd8a",
        "repo_digest": (
            "cybergym/oss-fuzz-base-runner@sha256:05e940b156bc21267a13159ff0606909fb112f189b10ad1a60afe6f2dce6bd8a"  # noqa: E501
        ),
    },
}

# The published binary-only corpus contains these six links. They are nested in
# directories that the upstream server bind-mounts at /out, so Docker preserves
# the absolute container path instead of resolving it on the host. Keep this an
# exact path-and-target allowlist: no other absolute grader-tree link is trusted.
_REVIEWED_CONTAINER_ABSOLUTE_SYMLINKS = {
    "arvo/60121/fix/out/oss-fuzz-zeek-scripts/tests": "/src/zeek/build/install-root/share/btest/data",
    "arvo/62356/fix/out/oss-fuzz-zeek-scripts/tests": "/src/zeek/build/install-root/share/btest/data",
    "arvo/65933/fix/out/oss-fuzz-zeek-scripts/tests": "/src/zeek/build/install-root/share/btest/data",
    "arvo/65933/vul/out/oss-fuzz-zeek-scripts/tests": "/src/zeek/build/install-root/share/btest/data",
    "arvo/66066/fix/out/oss-fuzz-zeek-scripts/tests": "/src/zeek/build/install-root/share/btest/data",
    "arvo/66066/vul/out/oss-fuzz-zeek-scripts/tests": "/src/zeek/build/install-root/share/btest/data",
}


class CatalogPreflightError(RuntimeError):
    pass


def _load_source_provenance(
    path: Path,
    *,
    data_dir: Path,
    require_root_owner: bool = True,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Load the frozen selective-HF manifest and return expected Level-1 hashes."""

    if (
        not path.is_file()
        or path.is_symlink()
        or path.stat().st_mode & 0o022
        or (require_root_owner and path.stat().st_uid != 0)
    ):
        raise CatalogPreflightError(f"source provenance must be a non-symlink, non-writable file: {path}")
    try:
        provenance_bytes = path.read_bytes()
        provenance = json.loads(provenance_bytes)
        manifest_path = Path(provenance["selected_manifest"])
        if manifest_path.resolve().parent != path.resolve().parent:
            raise ValueError("selected manifest must be colocated with provenance")
        if (
            not manifest_path.is_file()
            or manifest_path.is_symlink()
            or manifest_path.stat().st_mode & 0o022
            or (require_root_owner and manifest_path.stat().st_uid != 0)
        ):
            raise ValueError("selected manifest is missing, symlinked, or writable")
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CatalogPreflightError(f"source provenance is malformed: {path}") from exc
    expected = {
        "status": "verified",
        "repository": SOURCE_REPOSITORY,
        "repository_type": "dataset",
        "revision": SOURCE_REVISION,
        "root": str(data_dir.parent.resolve()),
        "file_count": SOURCE_FILE_COUNT,
        "total_bytes": SOURCE_TOTAL_BYTES,
        "git_objects_verified": 3,
        "lfs_xet_files_verified": 3_014,
        "gzip_tar_archives_verified": 1_507,
        "pointer_files_found": 0,
    }
    drift = {
        key: {"expected": value, "observed": provenance.get(key)}
        for key, value in expected.items()
        if provenance.get(key) != value
    }
    task_catalog = provenance.get("task_catalog")
    if (
        not isinstance(task_catalog, dict)
        or task_catalog.get("count") != 1_507
        or task_catalog.get("unique_count") != 1_507
    ):
        drift["task_catalog"] = {"expected": {"count": 1_507, "unique_count": 1_507}, "observed": task_catalog}
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if provenance.get("selected_manifest_sha256") != manifest_sha256:
        drift["selected_manifest_sha256"] = {
            "expected": provenance.get("selected_manifest_sha256"),
            "observed": manifest_sha256,
        }
    if (
        not isinstance(manifest, dict)
        or manifest.get("repository") != SOURCE_REPOSITORY
        or manifest.get("revision") != SOURCE_REVISION
    ):
        drift["selected_manifest"] = {"expected": SOURCE_REVISION, "observed": manifest}
    if drift:
        raise CatalogPreflightError(f"source provenance does not match the pinned corpus: {drift}")

    hashes: dict[str, str] = {}
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != SOURCE_FILE_COUNT:
        raise CatalogPreflightError("source selected manifest has the wrong file count")
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise CatalogPreflightError("source selected manifest contains a malformed file row")
        relative = item["path"]
        if not (relative.endswith("/description.txt") or relative.endswith("/repo-vul.tar.gz")):
            continue
        lfs = item.get("lfs")
        digest = lfs.get("sha256") if isinstance(lfs, dict) else None
        if not isinstance(digest, str) or len(digest) != 64:
            raise CatalogPreflightError(f"source selected manifest lacks an LFS SHA-256: {relative}")
        if relative in hashes:
            raise CatalogPreflightError(f"source selected manifest duplicates a path: {relative}")
        hashes[relative] = digest
    if len(hashes) != 3_014:
        raise CatalogPreflightError("source selected manifest does not cover every Level-1 task artifact")
    return hashes, {
        "source_revision": SOURCE_REVISION,
        "source_provenance_sha256": hashlib.sha256(provenance_bytes).hexdigest(),
        "source_selected_manifest_sha256": manifest_sha256,
    }


def _is_lfs_pointer(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(43) == b"version https://git-lfs.github.com/spec/v1"
    except OSError:
        return False


def _summarize_errors(errors: list[str]) -> str:
    visible = errors[:20]
    suffix = f"; ... and {len(errors) - len(visible)} more" if len(errors) > len(visible) else ""
    return "; ".join(visible) + suffix


def _stable_server_attestation(attestation: dict[str, Any]) -> dict[str, Any]:
    """Retain reviewed deployment identity, not restart-specific process IDs."""

    stable_fields = (
        "schema_version",
        "unit",
        "repository_revision",
        "repository_tree",
        "server_helper_sha256",
        "unit_fragments",
        "binary_dir",
        "host",
        "port",
        "service_user",
        "service_group",
    )
    return {key: attestation[key] for key in stable_fields if key in attestation}


def _binary_runner_identity_errors(image_identities: dict[str, str | None]) -> list[str]:
    errors: list[str] = []
    if set(image_identities) != set(REVIEWED_BINARY_RUNNER_IDENTITIES):
        errors.append("binary grader runner image references differ from the reviewed snapshot")
    for reference, expected in REVIEWED_BINARY_RUNNER_IDENTITIES.items():
        try:
            observed = json.loads(image_identities.get(reference) or "null")
        except json.JSONDecodeError:
            observed = None
        if not (
            isinstance(observed, dict)
            and observed.get("id") == expected["id"]
            and expected["repo_digest"] in (observed.get("repo_digests") or [])
            and observed.get("os") == "linux"
            and observed.get("architecture") == "amd64"
        ):
            errors.append(f"binary grader runner image identity drifted: {reference}")
    return errors


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
    """Fingerprint names, modes, symlink targets, and bytes under a grader tree."""

    digest = hashlib.sha256()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        stat_result = path.lstat()
        digest.update(f"{relative}\0{stat_result.st_mode & 0o7777:o}\0".encode())
        if path.is_symlink():
            digest.update(f"link\0{os.readlink(path)}\n".encode())
        elif path.is_file():
            digest.update(f"file\0{_sha256_file(path)}\n".encode())
        elif path.is_dir():
            digest.update(b"dir\n")
        else:
            digest.update(b"other\n")
    return digest.hexdigest()


def _require_reviewed_binary_tree(digest: str) -> None:
    if digest != REVIEWED_BINARY_TREE_SHA256:
        raise CatalogPreflightError(
            "binary grader tree does not match the reviewed deployment snapshot: "
            f"expected={REVIEWED_BINARY_TREE_SHA256}, observed={digest}"
        )


def _require_root_controlled_ancestors(root: Path) -> None:
    """Prevent an unprivileged operator from replacing a protected root."""

    current = root.parent
    while True:
        try:
            stat_result = current.lstat()
            xattrs = os.listxattr(current, follow_symlinks=False)
        except OSError as exc:
            raise CatalogPreflightError(f"could not verify protected ancestor: {current}") from exc
        if current.is_symlink() or stat_result.st_uid != 0 or stat_result.st_mode & 0o022:
            raise CatalogPreflightError(f"protected data ancestor is replaceable: {current}")
        if any(name in {"system.posix_acl_access", "system.posix_acl_default"} for name in xattrs):
            raise CatalogPreflightError(f"protected data ancestor has a POSIX ACL: {current}")
        if current == current.parent:
            return
        current = current.parent


def _reviewed_binary_manifest() -> dict[str, Any]:
    path = Path(__file__).with_name("binary-grader-manifest.json")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogPreflightError("reviewed binary grader manifest is missing or malformed") from exc
    if not isinstance(manifest, dict) or manifest.get("tree_sha256") != REVIEWED_BINARY_TREE_SHA256:
        raise CatalogPreflightError("reviewed binary grader manifest identity drifted")
    return manifest


def _require_protected_binary_tree(root: Path) -> int:
    """Require a root-owned tree that the unprivileged grader cannot mutate."""

    try:
        _require_root_controlled_ancestors(root)
        return validate_binary_tree_immutability(root)["entries"]
    except GraderAttestationError as exc:
        raise CatalogPreflightError(f"binary grader tree is not immutable: {root}") from exc


def _require_protected_source_tree(root: Path) -> int:
    """Require immutable task inputs while allowing group-readable files."""

    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise CatalogPreflightError("source data root must be an absolute, non-symlink directory")
    _require_root_controlled_ancestors(root)
    count = 0
    try:
        for path in chain((root,), root.rglob("*")):
            stat_result = path.lstat()
            if path.is_symlink():
                raise CatalogPreflightError(f"source data tree contains a symlink: {path}")
            if stat_result.st_uid != 0:
                raise CatalogPreflightError(f"source data node is not root-owned: {path}")
            if stat_result.st_mode & 0o022:
                raise CatalogPreflightError(f"source data node is group/world-writable: {path}")
            if any(
                name in {"system.posix_acl_access", "system.posix_acl_default"}
                for name in os.listxattr(path, follow_symlinks=False)
            ):
                raise CatalogPreflightError(f"source data node has a POSIX ACL: {path}")
            count += 1
    except OSError as exc:
        raise CatalogPreflightError(f"could not verify source data protection: {root}") from exc
    return count


def _validate_binary_tree_symlinks(root: Path) -> tuple[dict[str, int], list[str]]:
    """Validate contained relative links and the published container-only links."""

    counts = {"total": 0, "relative": 0, "reviewed_absolute": 0}
    errors: list[str] = []
    if not root.is_dir():
        return counts, errors

    root_resolved = root.resolve(strict=True)
    links = sorted(
        (path for path in root.rglob("*") if path.is_symlink()),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    counts["total"] = len(links)
    for path in links:
        relative = path.relative_to(root).as_posix()
        try:
            raw_target = os.readlink(path)
        except OSError:
            errors.append(f"unreadable binary grader symlink: {relative}")
            continue
        target = Path(raw_target)
        if target.is_absolute():
            expected = _REVIEWED_CONTAINER_ABSOLUTE_SYMLINKS.get(relative)
            if expected != raw_target:
                errors.append(f"unsupported absolute binary grader symlink: {relative} -> {raw_target}")
                continue
            counts["reviewed_absolute"] += 1
            continue

        candidate = path.parent / target
        try:
            non_strict_target = candidate.resolve(strict=False)
            non_strict_target.relative_to(root_resolved)
        except (OSError, RuntimeError, ValueError):
            errors.append(f"escaping relative binary grader symlink: {relative} -> {raw_target}")
            continue
        try:
            resolved_target = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            errors.append(f"broken relative binary grader symlink: {relative} -> {raw_target}")
            continue
        try:
            resolved_target.relative_to(root_resolved)
        except ValueError:
            errors.append(f"escaping relative binary grader symlink: {relative} -> {raw_target}")
            continue
        if not (resolved_target.is_file() or resolved_target.is_dir()):
            errors.append(f"unsupported relative binary grader symlink target: {relative} -> {raw_target}")
            continue
        counts["relative"] += 1
    return counts, errors


def _validate_capacity(*, max_concurrent: int, cpu_count: int, memory_bytes: int) -> dict[str, int]:
    if not 1 <= max_concurrent <= 6:
        raise CatalogPreflightError("campaign concurrency must be between 1 and 6")
    required_cpu = CPU_PER_ROLLOUT * max_concurrent
    required_memory = MEMORY_PER_ROLLOUT_BYTES * max_concurrent + HOST_MEMORY_RESERVE_BYTES
    if cpu_count < required_cpu or memory_bytes < required_memory:
        raise CatalogPreflightError(
            "worker is undersized for requested rolling concurrency: "
            f"requested={max_concurrent}, available_cpu={cpu_count}, required_cpu={required_cpu}, "
            f"available_memory_bytes={memory_bytes}, required_memory_bytes={required_memory}"
        )
    return {
        "cpu_count": cpu_count,
        "memory_bytes": memory_bytes,
        "required_cpu": required_cpu,
        "required_memory_bytes": required_memory,
    }


def _attest_live_server(
    *,
    repository_root: Path,
    server_url: str,
    server_mode: Literal["images", "binary"],
    server_binary_dir: Path | None,
    service: str = "cybergym-server.service",
    proc_root: Path = Path("/proc"),
    run_command: Callable[..., Any] = subprocess.run,
    deployment_seal: Path | None = None,
) -> dict[str, Any]:
    """Bind the local authenticated endpoint to the reviewed service command."""

    parsed = urlparse(server_url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.port is None or parsed.username or parsed.password:
        raise CatalogPreflightError("campaign server URL must be a plain private HTTP host and port")
    try:
        active = run_command(
            ["systemctl", "is-active", "--quiet", service],
            check=False,
            capture_output=True,
        )
        pid_text = run_command(
            ["systemctl", "show", service, "--property", "MainPID", "--value"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        pid = int(pid_text)
        command = (proc_root / str(pid) / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
        cwd = (proc_root / str(pid) / "cwd").resolve(strict=True)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError) as exc:
        raise CatalogPreflightError(f"could not attest live {service}") from exc
    if active.returncode != 0 or pid <= 0 or cwd != repository_root.resolve():
        raise CatalogPreflightError(f"{service} is inactive or running from an unexpected checkout")

    def option(name: str) -> str | None:
        positions = [index for index, value in enumerate(command) if value == name]
        if not positions:
            return None
        if len(positions) != 1 or positions[0] + 1 >= len(command):
            raise CatalogPreflightError(f"live {service} has a malformed {name} option")
        return command[positions[0] + 1]

    module_ok = any(command[index : index + 3] == ["python", "-m", "cybergym.server"] for index in range(len(command)))
    project = option("--project")
    host = option("--host")
    port = option("--port")
    binary_dir = option("--binary_dir")
    expected_binary_dir = str(server_binary_dir.resolve()) if server_mode == "binary" and server_binary_dir else None
    if (
        not module_ok
        or project != str((repository_root / "integrations/hud").resolve())
        or host != parsed.hostname
        or port != str(parsed.port)
        or binary_dir != expected_binary_dir
        or "--mask_map_path" in command
    ):
        raise CatalogPreflightError(f"live {service} command does not match the selected grader profile")
    if server_mode == "binary":
        if server_binary_dir is None or deployment_seal is None:
            raise CatalogPreflightError("binary campaign requires a root-owned live grader deployment seal")
        try:
            return attest_live_binary_server(
                deployment_seal=load_deployment_seal(deployment_seal),
                repository_root=repository_root.resolve(),
                binary_dir=server_binary_dir.resolve(),
                host=parsed.hostname,
                port=parsed.port,
                unit=service,
                proc_root=proc_root,
            )
        except GraderAttestationError as exc:
            raise CatalogPreflightError(f"live {service} failed process/listener attestation") from exc
    return {
        "service": service,
        "repository_root": str(repository_root.resolve()),
        "project": project,
        "module": "cybergym.server",
        "host": host,
        "port": parsed.port,
        "server_mode": server_mode,
        "binary_dir": binary_dir,
        "mask_map": False,
    }


def validate_full_catalog(
    *,
    repository_root: Path,
    data_dir: Path,
    source_provenance: Path | None,
    server_mode: Literal["images", "binary"],
    server_binary_dir: Path | None,
    max_concurrent: int,
    image_identity: Callable[[str], str | None],
    runtime_limit_probe: Callable[[], dict[str, int]],
    server_attestation: dict[str, Any],
    cpu_count: int,
    memory_bytes: int,
) -> dict[str, Any]:
    """Check all source/grader bytes without making an inference request."""

    catalog = task_ids(repository_root)
    expected_source_hashes: dict[str, str] = {}
    source_provenance_fields: dict[str, Any] = {}
    if source_provenance is not None:
        expected_source_hashes, source_provenance_fields = _load_source_provenance(
            source_provenance,
            data_dir=data_dir,
        )
        source_provenance_fields["protected_source_node_count"] = _require_protected_source_tree(data_dir)
    capacity = _validate_capacity(
        max_concurrent=max_concurrent,
        cpu_count=cpu_count,
        memory_bytes=memory_bytes,
    )
    runtime_limits = runtime_limit_probe()
    if runtime_limits != EXPECTED_RUNTIME_LIMITS:
        raise CatalogPreflightError(
            "Docker did not preserve the paid campaign's runtime limits: "
            f"expected={EXPECTED_RUNTIME_LIMITS}, observed={runtime_limits}"
        )
    errors: list[str] = []
    image_refs: set[str] = set()
    tar_count = 0
    source_digest = hashlib.sha256()
    symlink_counts = {"total": 0, "relative": 0, "reviewed_absolute": 0}
    if server_mode == "binary" and server_binary_dir is not None:
        symlink_counts, symlink_errors = _validate_binary_tree_symlinks(server_binary_dir)
        errors.extend(symlink_errors)
        if len(catalog) == 1_507 and symlink_counts["reviewed_absolute"] != len(_REVIEWED_CONTAINER_ABSOLUTE_SYMLINKS):
            errors.append(
                "binary grader tree is missing one or more reviewed container-only symlinks: "
                f"observed={symlink_counts['reviewed_absolute']}, "
                f"expected={len(_REVIEWED_CONTAINER_ABSOLUTE_SYMLINKS)}"
            )

    for task_id in catalog:
        subset, subid = task_id.split(":", 1)
        if not subid.isdigit():
            errors.append(f"non-numeric task suffix: {task_id}")
            continue
        task_data = data_dir / subset / subid
        description = task_data / "description.txt"
        archive = task_data / "repo-vul.tar.gz"
        if not description.is_file():
            errors.append(f"missing description: {task_id}")
        elif _is_lfs_pointer(description):
            errors.append(f"unresolved description LFS pointer: {task_id}")
        else:
            try:
                description_sha256 = _sha256_file(description)
                selected_path = f"data/{subset}/{subid}/description.txt"
                if expected_source_hashes and expected_source_hashes.get(selected_path) != description_sha256:
                    errors.append(f"description does not match pinned source manifest: {task_id}")
                source_digest.update(f"{task_id}\0description.txt\0{description_sha256}\n".encode())
            except OSError:
                errors.append(f"unreadable description: {task_id}")
        if not archive.is_file():
            errors.append(f"missing vulnerable archive: {task_id}")
        elif _is_lfs_pointer(archive):
            errors.append(f"unresolved archive LFS pointer: {task_id}")
        else:
            try:
                archive_sha256 = _sha256_file(archive)
                selected_path = f"data/{subset}/{subid}/repo-vul.tar.gz"
                if expected_source_hashes and expected_source_hashes.get(selected_path) != archive_sha256:
                    errors.append(f"archive does not match pinned source manifest: {task_id}")
                with tarfile.open(archive, "r:gz") as source:
                    for _member in source:
                        pass
                source_digest.update(f"{task_id}\0repo-vul.tar.gz\0{archive_sha256}\n".encode())
                tar_count += 1
            except (OSError, tarfile.TarError, EOFError):
                errors.append(f"unreadable vulnerable archive: {task_id}")

        if server_mode == "images":
            if subset == "arvo":
                image_refs.update((f"n132/arvo:{subid}-vul", f"n132/arvo:{subid}-fix"))
            else:
                image_refs.update((f"cybergym/oss-fuzz:{subid}-vul", f"cybergym/oss-fuzz:{subid}-fix"))
            continue

        if server_binary_dir is None:
            errors.append("binary mode requires --server-binary-dir")
            break
        binary_task = server_binary_dir / subset / subid
        if subset == "arvo":
            for variant in ("vul", "fix"):
                target = binary_task / variant
                binary = target / "arvo"
                if not binary.is_file() or not os.access(binary, os.X_OK):
                    errors.append(f"missing executable binary target: {task_id}/{variant}")
                if not (target / "out").is_dir():
                    errors.append(f"missing binary output directory: {task_id}/{variant}")
                if not (target / "libs").is_dir():
                    errors.append(f"missing binary library directory: {task_id}/{variant}")
                runner_file = target / "runner"
                if runner_file.is_file():
                    try:
                        runner_ref = runner_file.read_text(encoding="utf-8").strip()
                    except OSError:
                        runner_ref = ""
                    if not runner_ref:
                        errors.append(f"empty runner image reference: {task_id}/{variant}")
                    else:
                        image_refs.add(runner_ref)
                else:
                    image_refs.add("cybergym/oss-fuzz-base-runner:latest")
        else:
            for variant in ("vul", "fix"):
                target = binary_task / variant
                metadata_path = target / "metadata.json"
                fuzz_target = ""
                if not metadata_path.is_file():
                    errors.append(f"missing binary metadata: {task_id}/{variant}")
                else:
                    try:
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                        fuzz_target = metadata.get("fuzz_target", "") if isinstance(metadata, dict) else ""
                    except (OSError, json.JSONDecodeError):
                        metadata = None
                    if not isinstance(metadata, dict) or not isinstance(fuzz_target, str) or not fuzz_target:
                        errors.append(f"invalid binary fuzz-target metadata: {task_id}/{variant}")
                out_dir = target / "out"
                if not out_dir.is_dir():
                    errors.append(f"missing binary output directory: {task_id}/{variant}")
                elif fuzz_target:
                    executable = out_dir / fuzz_target
                    if not executable.is_file() or not os.access(executable, os.X_OK):
                        errors.append(f"missing executable fuzz target: {task_id}/{variant}/{fuzz_target}")
            image_refs.add("cybergym/oss-fuzz-base-runner:latest")

    image_identities = {ref: image_identity(ref) for ref in sorted(image_refs)}
    missing_images = sorted(ref for ref, identity in image_identities.items() if identity is None)
    errors.extend(f"missing Docker image: {ref}" for ref in missing_images)
    if server_mode == "binary" and len(catalog) == 1_507:
        errors.extend(_binary_runner_identity_errors(image_identities))
    if errors:
        raise CatalogPreflightError(
            f"full-corpus preflight found {len(errors)} problem(s): {_summarize_errors(errors)}"
        )

    binary_tree_sha256: str | None = None
    if server_mode == "binary" and server_binary_dir is not None:
        binary_tree_sha256 = _tree_digest(server_binary_dir)
        # Production full-catalog runs must use the byte-for-byte reviewed
        # snapshot. Small synthetic unit catalogs intentionally exercise the
        # structural validator without pretending to be that deployment.
        if len(catalog) == 1_507:
            protected_binary_node_count = _require_protected_binary_tree(server_binary_dir)
            manifest = _reviewed_binary_manifest()
            _require_reviewed_binary_tree(binary_tree_sha256)
            if protected_binary_node_count != manifest.get("entries") + 1:
                raise CatalogPreflightError(
                    "binary grader tree inventory does not match reviewed manifest: "
                    f"expected_nodes={manifest.get('entries', 0) + 1}, observed_nodes={protected_binary_node_count}"
                )
        else:
            protected_binary_node_count = None
    else:
        protected_binary_node_count = None

    digest = hashlib.sha256("\n".join(catalog).encode()).hexdigest()
    grader_digest = hashlib.sha256()
    grader_digest.update(f"mode\0{server_mode}\n".encode())
    stable_server = json.dumps(
        _stable_server_attestation(server_attestation),
        sort_keys=True,
        separators=(",", ":"),
    )
    grader_digest.update(f"server\0{stable_server}\n".encode())
    for reference, identity in image_identities.items():
        grader_digest.update(f"image\0{reference}\0{identity}\n".encode())
    if binary_tree_sha256 is not None:
        grader_digest.update(f"binary-tree\0{binary_tree_sha256}\n".encode())
    return {
        "schema_version": "1",
        "no_model_call": True,
        "completed_at": datetime.now(UTC).isoformat(),
        "catalog_sha256": digest,
        "source_artifact_sha256": source_digest.hexdigest(),
        "grader_artifact_sha256": grader_digest.hexdigest(),
        "binary_tree_sha256": binary_tree_sha256,
        "protected_binary_node_count": protected_binary_node_count,
        "task_count": len(catalog),
        "validated_tar_count": tar_count,
        "grader_server_mode": server_mode,
        "validated_image_count": len(image_refs),
        "validated_binary_symlink_count": symlink_counts["total"],
        "validated_relative_binary_symlink_count": symlink_counts["relative"],
        "validated_reviewed_absolute_binary_symlink_count": symlink_counts["reviewed_absolute"],
        "max_concurrent": max_concurrent,
        "capacity": capacity,
        "runtime_limits": runtime_limits,
        "server_attestation": server_attestation,
        **source_provenance_fields,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate all CyberGym assets without a model call")
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--source-provenance", type=Path, required=True)
    parser.add_argument("--server-mode", choices=("images", "binary"), required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--server-binary-dir", type=Path)
    parser.add_argument("--server-deployment-seal", type=Path)
    parser.add_argument("--max-concurrent", type=int, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    client = docker.from_env()
    try:
        info = client.info()

        def image_identity(reference: str) -> str | None:
            try:
                image = client.images.get(reference)
                repo_digests = sorted(image.attrs.get("RepoDigests") or [])
                return json.dumps(
                    {
                        "id": image.id,
                        "repo_digests": repo_digests,
                        "os": image.attrs.get("Os"),
                        "architecture": image.attrs.get("Architecture"),
                    },
                    sort_keys=True,
                )
            except docker.errors.ImageNotFound:
                return None

        def runtime_limit_probe() -> dict[str, int]:
            container = client.containers.create(
                OPENHANDS_RUNTIME_IMAGE,
                command=["/bin/true"],
                entrypoint=[],
                network_disabled=True,
                nano_cpus=CAMPAIGN_RUNTIME_NANO_CPUS,
                mem_limit=CAMPAIGN_RUNTIME_MEMORY_BYTES,
                memswap_limit=CAMPAIGN_RUNTIME_MEMORY_SWAP_BYTES,
                labels={"ai.hud.cybergym.preflight": "runtime-limits"},
            )
            try:
                container.reload()
                host_config = container.attrs.get("HostConfig") or {}
                return {
                    "nano_cpus": int(host_config.get("NanoCpus") or 0),
                    "memory": int(host_config.get("Memory") or 0),
                    "memory_swap": int(host_config.get("MemorySwap") or 0),
                }
            finally:
                container.remove(force=True)

        result = validate_full_catalog(
            repository_root=args.repository_root.expanduser().resolve(),
            data_dir=args.data_dir.expanduser().resolve(),
            source_provenance=args.source_provenance.expanduser().resolve(),
            server_mode=args.server_mode,
            server_binary_dir=(args.server_binary_dir.expanduser().resolve() if args.server_binary_dir else None),
            max_concurrent=args.max_concurrent,
            image_identity=image_identity,
            runtime_limit_probe=runtime_limit_probe,
            server_attestation=_attest_live_server(
                repository_root=args.repository_root.expanduser().resolve(),
                server_url=args.server,
                server_mode=args.server_mode,
                server_binary_dir=(args.server_binary_dir.expanduser().resolve() if args.server_binary_dir else None),
                deployment_seal=(
                    args.server_deployment_seal.expanduser().resolve() if args.server_deployment_seal else None
                ),
            ),
            cpu_count=int(info.get("NCPU") or 0),
            memory_bytes=int(info.get("MemTotal") or 0),
        )
    except CatalogPreflightError as exc:
        raise SystemExit(f"full-corpus preflight: {exc}") from exc
    finally:
        client.close()
    write_summary(args.report.expanduser().resolve(), result)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

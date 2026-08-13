"""No-inference validation of every CyberGym task artifact and worker capacity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import docker

from .scheduler import write_summary
from .taskset import task_ids

CPU_PER_ROLLOUT = 4
MEMORY_PER_ROLLOUT_BYTES = 8 * 1024**3
HOST_MEMORY_RESERVE_BYTES = 2 * 1024**3


class CatalogPreflightError(RuntimeError):
    pass


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


def validate_full_catalog(
    *,
    repository_root: Path,
    data_dir: Path,
    server_mode: Literal["images", "binary"],
    server_binary_dir: Path | None,
    max_concurrent: int,
    image_identity: Callable[[str], str | None],
    cpu_count: int,
    memory_bytes: int,
) -> dict[str, Any]:
    """Check all source/grader bytes without making an inference request."""

    catalog = task_ids(repository_root)
    capacity = _validate_capacity(
        max_concurrent=max_concurrent,
        cpu_count=cpu_count,
        memory_bytes=memory_bytes,
    )
    errors: list[str] = []
    image_refs: set[str] = set()
    tar_count = 0
    source_digest = hashlib.sha256()

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
                source_digest.update(f"{task_id}\0description.txt\0{_sha256_file(description)}\n".encode())
            except OSError:
                errors.append(f"unreadable description: {task_id}")
        if not archive.is_file():
            errors.append(f"missing vulnerable archive: {task_id}")
        elif _is_lfs_pointer(archive):
            errors.append(f"unresolved archive LFS pointer: {task_id}")
        else:
            try:
                archive_sha256 = _sha256_file(archive)
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
                if not (target / "metadata.json").is_file():
                    errors.append(f"missing binary metadata: {task_id}/{variant}")
                if not (target / "out").is_dir():
                    errors.append(f"missing binary output directory: {task_id}/{variant}")
            image_refs.add("cybergym/oss-fuzz-base-runner:latest")

    if server_mode == "binary" and server_binary_dir is not None:
        symlinks = [path for path in server_binary_dir.rglob("*") if path.is_symlink()]
        errors.extend(
            f"binary grader tree contains unsupported symlink: {path.relative_to(server_binary_dir)}"
            for path in symlinks
        )

    image_identities = {ref: image_identity(ref) for ref in sorted(image_refs)}
    missing_images = sorted(ref for ref, identity in image_identities.items() if identity is None)
    errors.extend(f"missing Docker image: {ref}" for ref in missing_images)
    if errors:
        raise CatalogPreflightError(
            f"full-corpus preflight found {len(errors)} problem(s): {_summarize_errors(errors)}"
        )

    digest = hashlib.sha256("\n".join(catalog).encode()).hexdigest()
    grader_digest = hashlib.sha256()
    grader_digest.update(f"mode\0{server_mode}\n".encode())
    for reference, identity in image_identities.items():
        grader_digest.update(f"image\0{reference}\0{identity}\n".encode())
    if server_mode == "binary" and server_binary_dir is not None:
        grader_digest.update(f"binary-tree\0{_tree_digest(server_binary_dir)}\n".encode())
    return {
        "schema_version": "1",
        "no_model_call": True,
        "completed_at": datetime.now(UTC).isoformat(),
        "catalog_sha256": digest,
        "source_artifact_sha256": source_digest.hexdigest(),
        "grader_artifact_sha256": grader_digest.hexdigest(),
        "task_count": len(catalog),
        "validated_tar_count": tar_count,
        "grader_server_mode": server_mode,
        "validated_image_count": len(image_refs),
        "max_concurrent": max_concurrent,
        "capacity": capacity,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate all CyberGym assets without a model call")
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--server-mode", choices=("images", "binary"), required=True)
    parser.add_argument("--server-binary-dir", type=Path)
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
                return json.dumps({"id": image.id, "repo_digests": repo_digests}, sort_keys=True)
            except docker.errors.ImageNotFound:
                return None

        result = validate_full_catalog(
            repository_root=args.repository_root.expanduser().resolve(),
            data_dir=args.data_dir.expanduser().resolve(),
            server_mode=args.server_mode,
            server_binary_dir=(args.server_binary_dir.expanduser().resolve() if args.server_binary_dir else None),
            max_concurrent=args.max_concurrent,
            image_identity=image_identity,
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

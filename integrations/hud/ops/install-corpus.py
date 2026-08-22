#!/usr/bin/env python3
"""Install the exact reviewed CyberGym source and binary grader corpora."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from urllib.parse import quote

SOURCE_REPOSITORY = "sunblaze-ucb/cybergym"
SOURCE_REVISION = "bde190ded494e52bc684b66073b436c9d992c7c6"
SOURCE_MANIFEST_SHA256 = "62020973579feafe340c756dd8e3aa0dc7d0e1e8b39674bd4063baa42c5a97ea"
SOURCE_PROVENANCE_SHA256 = "9246b82aa98f2f1afcede95f9045fae4429a8da7289966bad2c728af70f48cb5"
SOURCE_ROOT = Path("/srv/cybergym-runtime/task-data/cybergym-data")
PROVENANCE_ROOT = Path("/srv/cybergym-runtime/task-data/provenance")
BINARY_ROOT = Path("/srv/cybergym/cybergym-server-data")
DOWNLOAD_ROOT = Path("/srv/cybergym/downloads")
BINARY_ARCHIVE_URL = (
    "https://huggingface.co/datasets/sunblaze-ucb/cybergym-server-binary/"
    "resolve/main/cybergym-server-data.7z?download=true"
)
BINARY_ARCHIVE_SIZE = 20_841_640_942
BINARY_ARCHIVE_SHA256 = "b7d1e455e8aef06202f8d2371b5edd522afb954b2c8d86edaaa6aa6c49e3ea9f"
BINARY_APPARENT_BYTES = 130_195_047_160
DISK_RESERVE_BYTES = 40 * 1024**3
BINARY_INSTALL_MARKER = DOWNLOAD_ROOT / "cybergym-server-data.install.sha256"
SOURCE_INSTALL_MARKER = DOWNLOAD_ROOT / "cybergym-source.install.sha256"
CURL = Path(shutil.which("curl") or "/usr/bin/curl")


class InstallError(RuntimeError):
    """The pinned corpus could not be installed safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _git_blob_sha1(path: Path, size: int) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {size}\0".encode())
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path, expected_sha256: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or _sha256(path) != expected_sha256:
        raise InstallError(f"reviewed metadata identity drifted: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"reviewed metadata is malformed: {path}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"reviewed metadata is not an object: {path}")
    return value


def _manifest_files(manifest: dict[str, object]) -> list[dict[str, object]]:
    if (
        manifest.get("repository") != SOURCE_REPOSITORY
        or manifest.get("repository_type") != "dataset"
        or manifest.get("revision") != SOURCE_REVISION
    ):
        raise InstallError("reviewed source manifest repository identity drifted")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != 3_017:
        raise InstallError("reviewed source manifest file count drifted")
    output: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in files:
        if not isinstance(row, dict):
            raise InstallError("reviewed source manifest contains a non-object row")
        raw_path = row.get("path")
        size = row.get("size")
        if not isinstance(raw_path, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise InstallError("reviewed source manifest contains a malformed row")
        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != raw_path or raw_path in seen:
            raise InstallError(f"reviewed source manifest contains an unsafe path: {raw_path!r}")
        seen.add(raw_path)
        output.append(row)
    return output


def _matches(path: Path, row: dict[str, object]) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != row["size"]:
        return False
    lfs = row.get("lfs")
    if isinstance(lfs, dict):
        expected = lfs.get("sha256")
        return isinstance(expected, str) and _sha256(path) == expected
    blob_id = row.get("blob_id")
    return isinstance(blob_id, str) and _git_blob_sha1(path, int(row["size"])) == blob_id


def _download_source(row: dict[str, object]) -> tuple[str, bool]:
    relative = str(row["path"])
    destination = SOURCE_ROOT / PurePosixPath(relative)
    if _matches(destination, row):
        return relative, False
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        destination.unlink()
    partial = destination.with_name(f".{destination.name}.part")
    if partial.is_symlink() or (partial.exists() and not partial.is_file()):
        raise InstallError(f"unsafe partial source artifact: {partial}")
    encoded_path = "/".join(quote(part, safe="") for part in PurePosixPath(relative).parts)
    url = f"https://huggingface.co/datasets/{SOURCE_REPOSITORY}/resolve/{SOURCE_REVISION}/{encoded_path}?download=true"
    result = subprocess.run(  # noqa: S603
        [
            str(CURL),
            "--location",
            "--fail",
            "--show-error",
            "--silent",
            "--retry",
            "8",
            "--retry-delay",
            "2",
            "--retry-all-errors",
            "--continue-at",
            "-",
            "--output",
            str(partial),
            url,
        ],
        check=False,
    )
    if result.returncode != 0 or not _matches(partial, row):
        raise InstallError(f"source artifact download failed verification: {relative}")
    os.chown(partial, 0, 0)
    os.chmod(partial, 0o444)
    os.replace(partial, destination)
    return relative, True


def _copy_reviewed_metadata(artifacts: Path) -> None:
    PROVENANCE_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name, digest in (
        ("PROVENANCE.json", SOURCE_PROVENANCE_SHA256),
        ("selected-manifest.json", SOURCE_MANIFEST_SHA256),
    ):
        source = artifacts / name
        if _sha256(source) != digest:
            raise InstallError(f"reviewed metadata identity drifted: {source}")
        destination = PROVENANCE_ROOT / name
        temporary = destination.with_name(f".{name}.{os.getpid()}.tmp")
        shutil.copyfile(source, temporary)
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o444)
        os.replace(temporary, destination)


def _normalize_public_source_permissions() -> None:
    """Make verified public task data traversable by the unprivileged runner."""

    roots = (SOURCE_ROOT.parent, SOURCE_ROOT, PROVENANCE_ROOT)
    for root in roots:
        os.chown(root, 0, 0)
        os.chmod(root, 0o755)  # noqa: S103 - verified public corpus must be traversable by the runner
    for path in SOURCE_ROOT.rglob("*"):
        os.chown(path, 0, 0, follow_symlinks=False)
        if path.is_dir() and not path.is_symlink():
            os.chmod(path, 0o755)  # noqa: S103 - verified public corpus must be traversable by the runner
        elif path.is_file() and not path.is_symlink():
            os.chmod(path, 0o444)
    for path in PROVENANCE_ROOT.iterdir():
        if path.is_file() and not path.is_symlink():
            os.chown(path, 0, 0)
            os.chmod(path, 0o444)


def _curl_verified(url: str, destination: Path, *, size: int, sha256: str) -> None:
    if destination.is_file() and not destination.is_symlink() and destination.stat().st_size == size:
        if _sha256(destination) == sha256:
            return
    partial = destination.with_name(f".{destination.name}.part")
    if partial.is_symlink() or (partial.exists() and not partial.is_file()):
        raise InstallError(f"unsafe partial download: {partial}")
    result = subprocess.run(  # noqa: S603
        [
            str(CURL),
            "--location",
            "--fail",
            "--show-error",
            "--silent",
            "--retry",
            "8",
            "--retry-delay",
            "2",
            "--retry-all-errors",
            "--continue-at",
            "-",
            "--output",
            str(partial),
            url,
        ],
        check=False,
    )
    if (
        result.returncode != 0
        or not partial.is_file()
        or partial.is_symlink()
        or partial.stat().st_size != size
        or _sha256(partial) != sha256
    ):
        raise InstallError(f"download failed verification: {destination.name}")
    os.chown(partial, 0, 0)
    os.chmod(partial, 0o444)
    os.replace(partial, destination)


def _install_binary() -> None:
    if (
        BINARY_ROOT.is_dir()
        and not BINARY_ROOT.is_symlink()
        and BINARY_INSTALL_MARKER.is_file()
        and not BINARY_INSTALL_MARKER.is_symlink()
        and BINARY_INSTALL_MARKER.read_text().strip() == BINARY_ARCHIVE_SHA256
    ):
        return
    if BINARY_ROOT.exists() or BINARY_ROOT.is_symlink():
        raise InstallError(
            f"binary grader root exists without the reviewed install marker; move it aside and retry: {BINARY_ROOT}"
        )
    DOWNLOAD_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    archive = DOWNLOAD_ROOT / "cybergym-server-data.7z"
    _curl_verified(
        BINARY_ARCHIVE_URL,
        archive,
        size=BINARY_ARCHIVE_SIZE,
        sha256=BINARY_ARCHIVE_SHA256,
    )
    staging = BINARY_ROOT.with_name(f".{BINARY_ROOT.name}.extracting")
    if staging.exists() or staging.is_symlink():
        shutil.rmtree(staging)
    staging.mkdir(mode=0o700)
    seven_zip = shutil.which("7zz") or shutil.which("7z")
    if seven_zip is None:
        raise InstallError("7z or 7zz is required to extract the reviewed binary grader")
    subprocess.run([seven_zip, "x", "-y", f"-o{staging}", str(archive)], check=True)  # noqa: S603
    candidates = [staging / "cybergym-server-data", staging]
    extracted = next((candidate for candidate in candidates if (candidate / "arvo").is_dir()), None)
    if extracted is None:
        raise InstallError("reviewed binary archive did not contain the expected grader tree")
    if extracted == staging:
        staging.rename(BINARY_ROOT)
    else:
        extracted.rename(BINARY_ROOT)
        staging.rmdir()
    for path in (BINARY_ROOT, *BINARY_ROOT.rglob("*")):
        try:
            os.chown(path, 0, 0, follow_symlinks=False)
            if not path.is_symlink():
                os.chmod(path, stat.S_IMODE(path.stat().st_mode) & ~0o022)
        except OSError as exc:
            raise InstallError(f"could not protect binary grader path: {path}") from exc
    BINARY_INSTALL_MARKER.write_text(f"{BINARY_ARCHIVE_SHA256}\n", encoding="ascii")
    os.chown(BINARY_INSTALL_MARKER, 0, 0)
    os.chmod(BINARY_INSTALL_MARKER, 0o444)


def _required_bytes(files: list[dict[str, object]], *, install_source: bool, install_binary: bool) -> int:
    required = 0
    if install_source and not _source_is_installed():
        required += sum(int(row["size"]) for row in files if not _matches(SOURCE_ROOT / str(row["path"]), row))
    if install_binary and not (BINARY_ROOT.is_dir() and BINARY_INSTALL_MARKER.is_file()):
        required += BINARY_ARCHIVE_SIZE + BINARY_APPARENT_BYTES
    return required


def _source_is_installed() -> bool:
    return (
        SOURCE_ROOT.is_dir()
        and not SOURCE_ROOT.is_symlink()
        and (PROVENANCE_ROOT / "PROVENANCE.json").is_file()
        and (PROVENANCE_ROOT / "selected-manifest.json").is_file()
        and SOURCE_INSTALL_MARKER.is_file()
        and not SOURCE_INSTALL_MARKER.is_symlink()
        and SOURCE_INSTALL_MARKER.read_text().strip() == SOURCE_MANIFEST_SHA256
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--source-only", action="store_true")
    selection.add_argument("--binary-only", action="store_true")
    parser.add_argument("--workers", type=int, default=16)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if os.geteuid() != 0:
        raise SystemExit("install-corpus must run as root")
    if not CURL.is_file() or not os.access(CURL, os.X_OK):
        raise SystemExit("install-corpus requires curl")
    if not 1 <= args.workers <= 32:
        raise SystemExit("--workers must be between 1 and 32")
    script_dir = Path(__file__).resolve().parent
    artifacts = script_dir.parent / "artifacts" / "cybergym-source"
    manifest = _load_json(artifacts / "selected-manifest.json", SOURCE_MANIFEST_SHA256)
    _load_json(artifacts / "PROVENANCE.json", SOURCE_PROVENANCE_SHA256)
    files = _manifest_files(manifest)
    install_source = not args.binary_only
    install_binary = not args.source_only
    required = _required_bytes(files, install_source=install_source, install_binary=install_binary)
    available = shutil.disk_usage("/srv").free
    if available < required + DISK_RESERVE_BYTES:
        raise SystemExit(
            "insufficient free disk for reviewed corpus: "
            f"required_with_reserve={required + DISK_RESERVE_BYTES}, available={available}"
        )
    if install_source and not _source_is_installed():
        DOWNLOAD_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
        SOURCE_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
        downloaded = 0
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(_download_source, row) for row in files]
            for completed, future in enumerate(as_completed(futures), start=1):
                relative, changed = future.result()
                downloaded += int(changed)
                if completed % 50 == 0 or completed == len(futures):
                    print(
                        f"source progress: verified={completed}/{len(futures)} downloaded={downloaded} last={relative}",
                        flush=True,
                    )
        _copy_reviewed_metadata(artifacts)
        SOURCE_INSTALL_MARKER.write_text(f"{SOURCE_MANIFEST_SHA256}\n", encoding="ascii")
        os.chown(SOURCE_INSTALL_MARKER, 0, 0)
        os.chmod(SOURCE_INSTALL_MARKER, 0o444)
    if install_source:
        _normalize_public_source_permissions()
    if install_binary:
        _install_binary()
    print(
        json.dumps(
            {
                "binary_root": str(BINARY_ROOT) if install_binary else None,
                "source_files": len(files) if install_source else None,
                "source_root": str(SOURCE_ROOT) if install_source else None,
                "status": "installed",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (InstallError, OSError, subprocess.SubprocessError) as exc:
        print(f"install-corpus: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

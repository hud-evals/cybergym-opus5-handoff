"""Private-file semantics for local disks and attached Daytona volumes."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


def is_trusted_artifact_volume_path(path: Path) -> bool:
    raw = os.environ.get("CG_DAYTONA_ARTIFACT_VOLUME_ROOT", "").strip()
    if not raw:
        return False
    root = Path(raw)
    try:
        info = root.lstat()
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=False)
    except OSError:
        return False
    return bool(
        root.is_absolute()
        and stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and resolved_path.is_relative_to(resolved_root)
    )


def enforce_private_file_mode(descriptor: int, path: Path) -> None:
    try:
        os.fchmod(descriptor, 0o600)
    except PermissionError as exc:
        if exc.errno != errno.EPERM or not is_trusted_artifact_volume_path(path):
            raise


def has_private_storage(path: Path, mode: int) -> bool:
    return not mode & 0o077 or is_trusted_artifact_volume_path(path)


__all__ = [
    "enforce_private_file_mode",
    "has_private_storage",
    "is_trusted_artifact_volume_path",
]

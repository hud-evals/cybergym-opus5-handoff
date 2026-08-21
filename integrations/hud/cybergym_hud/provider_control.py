"""Persistent fleet-wide Anthropic credit admission control."""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .campaign import CampaignBlocked
from .scheduler import write_summary

SCHEMA = "cybergym.provider-credit-control.v1"


@dataclass(frozen=True, slots=True)
class ProviderProbeLease:
    token: str


@contextmanager
def _locked(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    descriptor = os.open(root / "provider-control.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _read(path: Path, kind: str) -> dict | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    if payload.get("schema") != SCHEMA or payload.get("kind") != kind:
        raise CampaignBlocked(f"provider control file is invalid: {path}")
    return payload


def _unlink(path: Path) -> None:
    if path.exists():
        path.unlink()


def wait_for_provider_admission(
    control_root: Path,
    *,
    poll_seconds: float = 5.0,
    probe_lease_seconds: float = 1800.0,
) -> ProviderProbeLease | None:
    """Return immediately when healthy, or lease exactly one recovery probe."""

    if poll_seconds <= 0 or probe_lease_seconds <= 0:
        raise ValueError("provider control intervals must be positive")
    root = control_root.expanduser().resolve()
    block_path = root / "provider-blocked.json"
    probe_path = root / "provider-probe.json"
    while True:
        delay = poll_seconds
        with _locked(root):
            blocked = _read(block_path, "credit-block")
            if blocked is None:
                return None
            retry_at = blocked.get("retry_at")
            if isinstance(retry_at, bool) or not isinstance(retry_at, int | float):
                raise CampaignBlocked("provider credit block has no numeric retry time")
            now = time.time()
            if now >= float(retry_at):
                probe = _read(probe_path, "credit-probe")
                if probe is None or float(probe.get("expires_at", 0)) <= now:
                    token = uuid4().hex
                    write_summary(
                        probe_path,
                        {
                            "schema": SCHEMA,
                            "kind": "credit-probe",
                            "token": token,
                            "created_at": now,
                            "expires_at": now + probe_lease_seconds,
                        },
                    )
                    return ProviderProbeLease(token)
                delay = min(poll_seconds, max(0.1, float(probe["expires_at"]) - now))
            else:
                delay = min(poll_seconds, max(0.1, float(retry_at) - now))
        time.sleep(delay)


def record_provider_result(
    control_root: Path,
    lease: ProviderProbeLease | None,
    *,
    credit_exhausted: bool,
    retry_seconds: float = 300.0,
) -> None:
    if retry_seconds <= 0:
        raise ValueError("provider retry delay must be positive")
    root = control_root.expanduser().resolve()
    block_path = root / "provider-blocked.json"
    probe_path = root / "provider-probe.json"
    with _locked(root):
        probe = _read(probe_path, "credit-probe")
        owns_probe = lease is not None and probe is not None and probe.get("token") == lease.token
        if credit_exhausted:
            retry_at = time.time() + retry_seconds
            prior = _read(block_path, "credit-block")
            if prior is not None and isinstance(prior.get("retry_at"), int | float):
                retry_at = max(retry_at, float(prior["retry_at"]))
            write_summary(
                block_path,
                {
                    "schema": SCHEMA,
                    "kind": "credit-block",
                    "provider": "anthropic",
                    "reason": "provider credit exhausted",
                    "retry_at": retry_at,
                },
            )
            if owns_probe:
                _unlink(probe_path)
        elif owns_probe:
            _unlink(block_path)
            _unlink(probe_path)


__all__ = ["ProviderProbeLease", "record_provider_result", "wait_for_provider_admission"]

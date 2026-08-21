from __future__ import annotations

import json
import time

from cybergym_hud.provider_control import (
    ProviderProbeLease,
    record_provider_result,
    wait_for_provider_admission,
)


def test_provider_credit_block_leases_one_probe_and_success_clears_it(tmp_path) -> None:
    root = tmp_path / "control"
    record_provider_result(root, None, credit_exhausted=True, retry_seconds=0.01)
    time.sleep(0.02)
    lease = wait_for_provider_admission(root, poll_seconds=0.01, probe_lease_seconds=1.0)
    assert isinstance(lease, ProviderProbeLease)
    probe = json.loads((root / "provider-probe.json").read_text())
    assert probe["token"] == lease.token
    record_provider_result(root, lease, credit_exhausted=False)
    assert not (root / "provider-blocked.json").exists()
    assert not (root / "provider-probe.json").exists()


def test_failed_probe_rearms_credit_block(tmp_path) -> None:
    root = tmp_path / "control"
    record_provider_result(root, None, credit_exhausted=True, retry_seconds=0.01)
    time.sleep(0.02)
    lease = wait_for_provider_admission(root, poll_seconds=0.01, probe_lease_seconds=1.0)
    record_provider_result(root, lease, credit_exhausted=True, retry_seconds=60.0)
    blocked = json.loads((root / "provider-blocked.json").read_text())
    assert blocked["retry_at"] > time.time() + 50
    assert not (root / "provider-probe.json").exists()

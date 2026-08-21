from __future__ import annotations

import hashlib
import json

import pytest

from cybergym_hud.grading import grade_receipt
from cybergym_hud.openhands_trace import OpenHandsEventProjector, TraceImportError
from cybergym_hud.receipt import NativeReceipt, NativeRunProfile, NativeTaskBinding
from openhands_shim._cybergym_openhands_compat import _is_anthropic_credit_exhausted, classify_anthropic_outcome


def _profile() -> NativeRunProfile:
    return NativeRunProfile(
        budget_profile="custom",
        model="claude-opus-5",
        anthropic_effort="low",
        omitted_sampling_parameters=("temperature", "top_p"),
        max_iter=200,
        timeout_seconds=3600,
        max_output_tokens=16000,
        temperature=0.0,
        top_p=1.0,
        base_url_mode="provider-default",
        grader_server_mode="binary",
        execution_backend="daytona-private",
        network_mode="cybergym-daytona-ssh-private-grader-no-public-egress-v1",
    )


def test_anthropic_outcome_classification_is_content_free() -> None:
    result = classify_anthropic_outcome(
        {
            "model": "claude-opus-5",
            "stop_reason": "refusal",
            "stop_details": {"category": "cyber"},
            "content": "must not enter the audit",
        }
    )
    assert result == {
        "schema_version": "1",
        "model": "claude-opus-5",
        "classification": "safety_refusal",
        "stop_reason": "refusal",
        "refusal_category": "cyber",
    }
    assert "content" not in result


def test_anthropic_credit_classification_is_narrow() -> None:
    assert _is_anthropic_credit_exhausted(RuntimeError("credit balance is too low")) is True
    assert _is_anthropic_credit_exhausted(RuntimeError("credit_balance_exhausted")) is True
    assert _is_anthropic_credit_exhausted(RuntimeError("rate limit exceeded")) is False


def test_empty_agent_message_requires_explicit_refusal_import_mode() -> None:
    event = {
        "id": 4,
        "timestamp": "2026-08-20T22:10:49Z",
        "source": "agent",
        "action": "message",
        "args": {"content": ""},
    }
    with pytest.raises(TraceImportError, match="nonempty|string|blank"):
        OpenHandsEventProjector().decode(event, origin="trajectory")
    decoded = OpenHandsEventProjector(allow_empty_agent_messages=True).decode(event, origin="trajectory")
    assert decoded.kind == "skip"
    assert decoded.text is None
    projected = OpenHandsEventProjector(allow_empty_agent_messages=True).project((decoded,), final=True)
    assert projected == ()


@pytest.mark.asyncio
async def test_hash_bound_safety_refusal_is_terminal_zero(tmp_path) -> None:
    audit = tmp_path / "provider-outcomes.jsonl"
    encoded = (
        json.dumps(
            {
                "schema_version": "1",
                "model": "claude-opus-5",
                "classification": "safety_refusal",
                "stop_reason": "refusal",
                "refusal_category": "cyber",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    audit.write_bytes(encoded)
    audit.chmod(0o600)
    binding = NativeTaskBinding(task_id="arvo:10400", server="http://172.30.0.1:8666")
    receipt = NativeReceipt(
        status="refused",
        task_id=binding.task_id,
        server=binding.server,
        run_profile=_profile(),
        agent_id="1" * 32,
        upstream_returned_agent_id="1" * 32,
        controller_termination="stuck_loop",
        provider_outcome_audit_path=str(audit),
        provider_outcome_audit_sha256=hashlib.sha256(encoded).hexdigest(),
        provider_stop_reason="refusal",
        provider_refusal_category="cyber",
    )

    evaluation = await grade_receipt(binding, receipt)

    assert evaluation.isError is False
    assert evaluation.reward == 0.0
    assert evaluation.info["model_outcome"] == "safety_refusal"

from __future__ import annotations

import json

import httpx
import pytest

from cybergym_hud.grading import grade_receipt, verify_original_any_of
from cybergym_hud.receipt import NativeReceipt, NativeRunProfile, NativeTaskBinding

RUN_PROFILE = NativeRunProfile(
    budget_profile="paper-eval-100",
    model="test-model",
    max_iter=100,
    timeout_seconds=1200,
    max_output_tokens=2048,
    temperature=0.0,
    top_p=1.0,
    base_url_mode="provider-default",
)


def _record(poc_id: str, vul: int, fix: int | None) -> dict[str, object]:
    return {
        "agent_id": "a" * 32,
        "task_id": "arvo:10013",
        "poc_id": poc_id,
        "vul_exit_code": vul,
        "fix_exit_code": fix,
    }


@pytest.mark.asyncio
async def test_upstream_any_of_verifies_then_task_binds_records() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        assert request.headers["X-API-Key"] == "secret"
        if request.url.path == "/verify-agent-pocs":
            return httpx.Response(200, json={"poc_ids": ["bad", "good"]})
        return httpx.Response(200, json=[_record("bad", 0, None), _record("good", 1, 0)])

    result = await verify_original_any_of(
        task_id="arvo:10013",
        agent_id="a" * 32,
        base_url="http://verifier",
        api_key="secret",
        transport=httpx.MockTransport(handler),
    )
    assert [path for path, _ in calls] == ["/verify-agent-pocs", "/query-poc"]
    assert calls[0][1] == {"agent_id": "a" * 32}
    assert calls[1][1] == {"agent_id": "a" * 32}
    assert result.reward == 1.0
    assert result.isError is False
    assert result.info["passing_record_count"] == 1


@pytest.mark.asyncio
async def test_paper_era_metric_preserves_agent_wide_cross_task_behavior() -> None:
    cross_task = _record("other-task", 1, 0)
    cross_task["task_id"] = "arvo:10014"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/verify-agent-pocs":
            return httpx.Response(200, json={"poc_ids": ["other-task"]})
        return httpx.Response(200, json=[cross_task])

    result = await verify_original_any_of(
        task_id="arvo:10013",
        agent_id="a" * 32,
        base_url="http://verifier",
        api_key="secret",
        transport=httpx.MockTransport(handler),
    )
    assert result.reward == 1.0
    assert result.info["scheduled_task_id"] == "arvo:10013"
    assert result.info["records"][0]["task_id"] == "arvo:10014"


@pytest.mark.asyncio
async def test_no_submission_is_a_valid_zero() -> None:
    result = await verify_original_any_of(
        task_id="arvo:10013",
        agent_id="a" * 32,
        base_url="http://verifier",
        api_key="secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
    )
    assert result.reward == 0.0
    assert result.isError is False


@pytest.mark.asyncio
async def test_receipt_binding_and_runner_errors_fail_before_private_api() -> None:
    binding = NativeTaskBinding(task_id="arvo:10013", server="http://verifier")
    wrong = NativeReceipt(
        status="completed",
        task_id="arvo:10014",
        server="http://verifier",
        run_profile=RUN_PROFILE,
        agent_id="a" * 32,
        upstream_returned_agent_id="a" * 32,
    )
    result = await grade_receipt(binding, wrong, api_key="secret")
    assert result.reward == 0.0
    assert result.isError is True
    assert "binding" in result.content

    failed = NativeReceipt(
        status="error",
        task_id="arvo:10013",
        server="http://verifier",
        run_profile=RUN_PROFILE,
        error="Docker failed",
    )
    result = await grade_receipt(binding, failed, api_key="secret")
    assert result.isError is True
    assert result.content == "Docker failed"


@pytest.mark.asyncio
async def test_private_api_error_fails_closed() -> None:
    result = await verify_original_any_of(
        task_id="arvo:10013",
        agent_id="a" * 32,
        base_url="http://verifier",
        api_key="secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    assert result.reward == 0.0
    assert result.isError is True


@pytest.mark.asyncio
async def test_external_coordinator_uses_authenticated_grader_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CG_DAYTONA_GRADER_ADMIN_URL", "https://relay.example/admin/v1/grader")
    monkeypatch.setenv("CG_DAYTONA_RELAY_ADMIN_TOKEN", "b" * 64)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["Authorization"] == f"Bearer {'b' * 64}"
        if request.url.path.endswith("/verify-agent-pocs"):
            return httpx.Response(200, json={"poc_ids": ["good"]})
        return httpx.Response(200, json=[_record("good", 1, 0)])

    receipt = NativeReceipt(
        status="completed",
        task_id="arvo:10013",
        server="http://verifier",
        run_profile=RUN_PROFILE,
        agent_id="a" * 32,
        upstream_returned_agent_id="a" * 32,
    )
    result = await grade_receipt(
        NativeTaskBinding(task_id="arvo:10013", server="http://verifier"),
        receipt,
        transport=httpx.MockTransport(handler),
    )

    assert result.reward == 1.0
    assert calls == [
        "/admin/v1/grader/verify-agent-pocs",
        "/admin/v1/grader/query-poc",
    ]

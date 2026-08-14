from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import mcp.types as mcp_types
import pytest
from hud.agents.types import AgentStep, ToolStep, Usage
from hud.types import MCPToolCall, MCPToolResult, Step

from cybergym_hud.trace_backfill import (
    BackfillLedger,
    HTTPBackfillTransport,
    KeyedStep,
    StepRedactor,
    TraceBackfillError,
    apply_backfill,
    build_backfill_plan,
    deterministic_span_id,
    main,
)

TRACE_ID = "90fd9767-083f-4286-a918-6bd3f6a2d764"
START = "2026-08-13T17:00:00+00:00"
END = "2026-08-13T17:00:01+00:00"


def _user(text: str = "repair the target") -> Step:
    return Step(
        source="user",
        messages=[
            mcp_types.PromptMessage(
                role="user",
                content=mcp_types.TextContent(type="text", text=text),
            )
        ],
        started_at=START,
        ended_at=END,
    )


def _agent(
    *,
    content: str = "",
    command: str = "ls",
    call_id: str = "call-1",
    arguments: dict[str, Any] | None = None,
) -> AgentStep:
    return AgentStep(
        content=content,
        tool_calls=[MCPToolCall(id=call_id, name="run", arguments=arguments or {"command": command})],
        started_at=START,
        ended_at=END,
    )


def _tool(
    *,
    command: str = "ls",
    result: str = "ok",
    call_id: str = "call-1",
    arguments: dict[str, Any] | None = None,
) -> ToolStep:
    call = MCPToolCall(id=call_id, name="run", arguments=arguments or {"command": command})
    return ToolStep(
        call=call,
        result=MCPToolResult(
            call_id=call_id,
            content=[mcp_types.TextContent(type="text", text=result)],
            isError=False,
        ),
        started_at=START,
        ended_at=END,
    )


def _done(content: str = "finished") -> AgentStep:
    return AgentStep(content=content, done=True, started_at=START, ended_at=END)


def _mapped(*, command: str = "ls") -> list[KeyedStep]:
    return [
        KeyedStep("user:prompt", _user()),
        KeyedStep("response:one", _agent(command=command)),
        KeyedStep("observation:one", _tool(command=command)),
        KeyedStep("response:final", _done()),
    ]


def _cybergym_projection_shape() -> list[KeyedStep]:
    """Structurally exact 1 user / 14 agents / 22 tools projector contract."""

    items = [KeyedStep("user:prompt", _user())]
    call_number = 0
    for response_number in range(13):
        call_count = 2 if response_number < 9 else 1
        calls: list[MCPToolCall] = []
        for _ in range(call_count):
            call_number += 1
            calls.append(
                MCPToolCall(
                    id=f"call-{call_number}",
                    name="run",
                    provider_name="run",
                    arguments={"command": f"command-{call_number}"},
                )
            )
        items.append(
            KeyedStep(
                f"response:{response_number}",
                AgentStep(tool_calls=calls, started_at=START, ended_at=END),
            )
        )
        for call in calls:
            items.append(
                KeyedStep(
                    f"tool:{call.id}",
                    ToolStep(
                        call=call,
                        result=MCPToolResult(
                            call_id=call.id,
                            content=[mcp_types.TextContent(type="text", text="ok")],
                        ),
                        started_at=START,
                        ended_at=END,
                    ),
                )
            )
    items.append(KeyedStep("response:final", _done()))
    return items


def _event_for_span(span: dict[str, Any]) -> dict[str, Any]:
    payload = span["attributes"]["hud.payload"]
    source = payload["source"]
    base = {"id": span["span_id"]}
    if source == "user":
        texts = [
            message["content"]["text"]
            for message in payload["messages"]
            if message.get("content", {}).get("type") == "text"
        ]
        return {**base, "kind": "user_message", "text": "\n\n".join(texts)}
    if source == "agent":
        return {
            **base,
            "kind": "agent_message",
            "text": payload.get("content"),
            "reasoning": payload.get("reasoning"),
            "tool_calls": [
                {
                    "tool_call_id": call.get("id"),
                    "name": call["name"],
                    "arguments": call.get("arguments") or {},
                }
                for call in payload.get("tool_calls", [])
            ],
        }
    if source == "tool":
        result = payload["result"]
        texts = [
            item["text"]
            for item in result.get("content", [])
            if item.get("type") == "text" and isinstance(item.get("text"), str)
        ]
        result_text = "\n".join(texts) if texts else None
        is_error = bool(result.get("isError"))
        return {
            **base,
            "kind": "tool_call",
            "tool_name": payload["call"]["name"],
            "arguments": payload["call"].get("arguments") or {},
            "result_text": None if is_error else result_text,
            "result_data": result if result_text is None else None,
            "error": (result_text or "tool error (no message)") if is_error else None,
        }
    raise AssertionError(source)


class FakeTransport:
    def __init__(
        self,
        *,
        status: str = "completed",
        events: list[dict[str, Any]] | None = None,
        fail_after_accept: bool = False,
    ) -> None:
        self.status = status
        self.events = list(events or [])
        self.fail_after_accept = fail_after_accept
        self.uploads: list[list[dict[str, Any]]] = []

    def fetch_events(self, _trace_id: str) -> dict[str, Any]:
        return {"status": self.status, "events": list(self.events), "latest_seq": len(self.events) - 1}

    def upload_spans(self, _trace_id: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
        self.uploads.append(list(spans))
        self.events.extend(_event_for_span(span) for span in spans)
        if self.fail_after_accept:
            raise OSError("simulated response loss")
        return {"status": "accepted", "count": len(spans), "sequence": len(self.events) - 1}


def _ledger(tmp_path: Path) -> BackfillLedger:
    state = tmp_path / "private-state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    return BackfillLedger(state / "ledger.json")


def test_plan_has_stable_trace_scoped_span_ids_and_no_content_in_summary() -> None:
    first = build_backfill_plan(TRACE_ID, _mapped(), redactor=StepRedactor())
    second = build_backfill_plan(TRACE_ID, _mapped(), redactor=StepRedactor())

    assert first.plan_sha256 == second.plan_sha256
    assert first.span_ids == second.span_ids
    assert first.span_ids[0] == deterministic_span_id(TRACE_ID, first.namespace, "user:prompt")
    assert len(set(first.span_ids)) == 4
    assert first.event_counts == {"user_message": 1, "agent_message": 2, "tool_call": 1}
    summary = json.dumps(first.public_summary(mode="dry-run"))
    assert "repair the target" not in summary
    assert first.public_summary(mode="dry-run")["network_write_performed"] is False


def test_mapper_may_return_its_own_structural_projected_step_type() -> None:
    @dataclass(frozen=True)
    class ProjectedStep:
        key: str
        step: Step

    plan = build_backfill_plan(
        TRACE_ID,
        [ProjectedStep(item.key, item.step) for item in _mapped()],
        redactor=StepRedactor(),
    )

    assert plan.event_counts == {"agent_message": 2, "tool_call": 1, "user_message": 1}


def test_span_id_stays_stable_for_same_key_but_plan_digest_detects_payload_drift() -> None:
    first = build_backfill_plan(TRACE_ID, _mapped(command="ls"), redactor=StepRedactor())
    changed = build_backfill_plan(TRACE_ID, _mapped(command="pwd"), redactor=StepRedactor())

    assert first.span_ids == changed.span_ids
    assert first.plan_sha256 != changed.plan_sha256


def test_redaction_covers_environment_literals_sensitive_keys_bearer_keys_and_flags() -> None:
    literal = "literal-secret-123456"
    provider_key = "sk-abcdefghijklmnopqrstuvwxyz012345"
    command = (
        f"OPENAI_API_KEY={literal} curl -H 'Authorization: Bearer abcdefghijklmnop' "
        f"--data {provider_key} flag{{never-upload-this}}"
    )
    arguments = {
        "command": command,
        "agent_id": "private-agent-id",
        "nested": {"client_secret": "private-client-secret"},
    }
    agent = _agent(arguments=arguments)
    plan = build_backfill_plan(
        TRACE_ID,
        [
            KeyedStep("user:prompt", _user()),
            KeyedStep("response:secret", agent),
            KeyedStep("tool:secret", _tool(arguments=arguments)),
            KeyedStep("response:final", _done()),
        ],
        redactor=StepRedactor([literal]),
    )

    wire = json.dumps(plan.spans)
    for forbidden in (
        literal,
        provider_key,
        "abcdefghijklmnop",
        "never-upload-this",
        "private-agent-id",
        "private-client-secret",
    ):
        assert forbidden not in wire
    assert plan.redactions["applied"] >= 6
    assert "sensitive_key" in plan.redactions["by_category"]


def test_wire_payload_is_a_strict_display_channel_whitelist() -> None:
    mapped = _mapped()
    call = mapped[1].step.tool_calls[0]
    call.provider_name = call.name
    mapped[1].step.model = "gpt-test"
    mapped[1].step.usage = Usage(prompt_tokens=2, completion_tokens=3)
    plan = build_backfill_plan(TRACE_ID, mapped, redactor=StepRedactor())

    user = plan.spans[0]["attributes"]["hud.payload"]
    agent = plan.spans[1]["attributes"]["hud.payload"]
    tool = plan.spans[2]["attributes"]["hud.payload"]
    assert set(user) == {"source", "messages", "started_at", "ended_at"}
    assert set(agent) == {
        "source",
        "content",
        "tool_calls",
        "done",
        "model",
        "usage",
        "started_at",
        "ended_at",
    }
    assert set(agent["tool_calls"][0]) == {"id", "name", "arguments"}
    assert set(tool) == {"source", "call", "result", "started_at", "ended_at"}
    assert set(tool["call"]) == {"id", "name", "arguments"}
    assert set(tool["result"]) == {"content", "isError"}
    assert set(tool["result"]["content"][0]) == {"type", "text"}
    forbidden = {"raw", "sample", "citations", "refusal", "messages", "extra", "error", "_meta"}
    assert not forbidden.intersection(agent)
    assert "provider_name" not in json.dumps(plan.spans)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda steps: setattr(steps[1].step, "raw", {"hidden": "secret"}), "raw/sample"),
        (lambda steps: steps[1].step.extra.update({"hidden": "secret"}), "forbidden metadata"),
        (lambda steps: setattr(steps[1].step, "error", "secret"), "forbidden metadata"),
        (
            lambda steps: setattr(
                steps[1].step,
                "tool_calls",
                [MCPToolCall(id="call-1", name="run", arguments={"command": "ls"}, hidden="secret")],
            ),
            "unknown metadata",
        ),
        (
            lambda steps: setattr(
                steps[2].step.result,
                "structuredContent",
                {"hidden": "secret"},
            ),
            "structured",
        ),
        (
            lambda steps: setattr(
                steps[2].step.result,
                "content",
                [mcp_types.ImageContent(type="image", data="secret", mimeType="image/png")],
            ),
            "text content only",
        ),
    ],
)
def test_mapper_hidden_or_nondisplay_channels_fail_closed(mutate: Any, message: str) -> None:
    mapped = _mapped()
    mutate(mapped)
    with pytest.raises(TraceBackfillError, match=message):
        build_backfill_plan(TRACE_ID, mapped, redactor=StepRedactor())


def test_recursive_redaction_covers_quoted_assignments_auth_jwt_cookies_and_provider_keys() -> None:
    exact = "literal-runtime-secret-987654"
    forbidden = [
        exact,
        "json-secret-value",
        "single-secret-value",
        "basiccredentialvalue",
        "eyJabcde.eyJfghij.signaturevalue",
        "cookie-secret",
        "second-cookie-secret",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "session-secret",
        "auth-secret",
        "flag-secret",
    ]
    command = "\n".join(
        [
            f'EXACT_TOKEN="{exact}"',
            '"OPENAI_API_KEY": "json-secret-value"',
            "'github_token'='single-secret-value'",
            "Authorization: Basic basiccredentialvalue",
            "Authorization: Bearer eyJabcde.eyJfghij.signaturevalue",
            "Cookie: sid=cookie-secret; csrf=second-cookie-secret",
            "ghp_abcdefghijklmnopqrstuvwxyz123456",
            "flag{flag-secret}",
        ]
    )
    arguments = {
        "command": command,
        "cookies": {"sid": "cookie-secret"},
        "session": "session-secret",
        "nested": {"auth": "auth-secret"},
    }
    plan = build_backfill_plan(
        TRACE_ID,
        [
            KeyedStep("user:prompt", _user()),
            KeyedStep("response:secret", _agent(arguments=arguments)),
            KeyedStep("tool:secret", _tool(arguments=arguments)),
            KeyedStep("response:final", _done()),
        ],
        redactor=StepRedactor([exact]),
    )
    wire = json.dumps(plan.spans)
    assert all(value not in wire for value in forbidden)
    assert plan.redactions["applied"] >= len(forbidden)


def test_environment_redactor_includes_generic_exact_and_suffixed_secret_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generic = "generic-environment-secret"
    suffixed = "suffixed-environment-secret"
    monkeypatch.setenv("TOKEN", generic)
    monkeypatch.setenv("CUSTOM_COOKIE", suffixed)
    arguments = {"command": f"print {generic} and {suffixed}"}
    plan = build_backfill_plan(
        TRACE_ID,
        [
            KeyedStep("user", _user()),
            KeyedStep("agent", _agent(arguments=arguments)),
            KeyedStep("tool", _tool(arguments=arguments)),
            KeyedStep("final", _done()),
        ],
    )
    wire = json.dumps(plan.spans)
    assert generic not in wire
    assert suffixed not in wire


def test_projector_contract_shape_is_1_user_14_agents_22_tools_with_strict_timestamps() -> None:
    plan = build_backfill_plan(
        TRACE_ID,
        _cybergym_projection_shape(),
        redactor=StepRedactor(),
    )

    assert plan.event_counts == {"user_message": 1, "agent_message": 14, "tool_call": 22}
    previous_end = None
    for span in plan.spans:
        started = datetime.fromisoformat(span["start_time"])
        ended = datetime.fromisoformat(span["end_time"])
        assert started < ended
        if previous_end is not None:
            assert previous_end < started
        previous_end = ended


@pytest.mark.parametrize(
    "mapped, message",
    [
        ([KeyedStep("same", _user()), KeyedStep("same", _user("other"))], "duplicate"),
        ({"blank": AgentStep(started_at=START, ended_at=END)}, "blank HUD agent"),
        ({"untimed": AgentStep(content="hello")}, "timestamps"),
        ({"blank-user": Step(source="user", started_at=START, ended_at=END)}, "blank HUD user"),
        ({"partial-tool": ToolStep(call=MCPToolCall(name="run"), started_at=START, ended_at=END)}, "complete"),
    ],
)
def test_plan_rejects_nondeterministic_or_blank_mapper_output(mapped: Any, message: str) -> None:
    with pytest.raises(TraceBackfillError, match=message):
        build_backfill_plan(TRACE_ID, mapped, redactor=StepRedactor())


@pytest.mark.parametrize(
    "mapped, message",
    [
        ([KeyedStep("response:final", _done())], "begin with exactly one user"),
        (
            [KeyedStep("user:one", _user()), KeyedStep("user:two", _user()), KeyedStep("final", _done())],
            "exactly one first user",
        ),
        (
            [KeyedStep("user", _user()), KeyedStep("agent", _agent()), KeyedStep("final", _done())],
            "pending tool results",
        ),
        (
            [KeyedStep("user", _user()), KeyedStep("tool", _tool()), KeyedStep("final", _done())],
            "orphan tool result",
        ),
        (
            [
                KeyedStep("user", _user()),
                KeyedStep("agent:one", _agent()),
                KeyedStep("tool:one", _tool()),
                KeyedStep("agent:two", _agent()),
                KeyedStep("tool:two", _tool()),
                KeyedStep("final", _done()),
            ],
            "duplicate tool call id",
        ),
        (
            [
                KeyedStep("user", _user()),
                KeyedStep("agent", _agent()),
                KeyedStep("tool", _tool(command="pwd")),
                KeyedStep("final", _done()),
            ],
            "does not match",
        ),
        (
            [
                KeyedStep("user", _user()),
                KeyedStep("agent", _agent()),
                KeyedStep("tool", _tool()),
            ],
            "terminal done assistant",
        ),
    ],
)
def test_cross_step_topology_fails_closed(mapped: list[KeyedStep], message: str) -> None:
    with pytest.raises(TraceBackfillError, match=message):
        build_backfill_plan(TRACE_ID, mapped, redactor=StepRedactor())


def test_topology_is_checked_before_sensitive_values_collapse_under_redaction() -> None:
    mapped = [
        KeyedStep("user", _user()),
        KeyedStep("agent", _agent(arguments={"token": "first-secret-value"})),
        KeyedStep("tool", _tool(arguments={"token": "different-secret-value"})),
        KeyedStep("final", _done()),
    ]
    with pytest.raises(TraceBackfillError, match="does not match"):
        build_backfill_plan(TRACE_ID, mapped, redactor=StepRedactor())


def test_apply_uploads_missing_spans_then_verifies_expected_agent_tool_counts(tmp_path: Path) -> None:
    plan = build_backfill_plan(TRACE_ID, _mapped(), redactor=StepRedactor())
    transport = FakeTransport()
    ledger = _ledger(tmp_path)

    summary = apply_backfill(plan, ledger=ledger, transport=transport, verify_timeout_seconds=0)

    assert len(transport.uploads) == 1
    assert len(transport.uploads[0]) == 4
    assert summary["remote_verified"] is True
    assert summary["uploaded_span_count"] == 4
    assert summary["remote_verified_event_counts"] == {
        "agent_message": 2,
        "tool_call": 1,
        "user_message": 1,
    }
    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(ledger.path.parent.stat().st_mode) == 0o700
    persisted = json.loads(ledger.path.read_text())
    assert persisted["traces"][TRACE_ID]["status"] == "verified"
    assert "repair the target" not in json.dumps(persisted)


def test_rerun_is_remote_idempotent_even_without_the_prior_ledger(tmp_path: Path) -> None:
    plan = build_backfill_plan(TRACE_ID, _mapped(), redactor=StepRedactor())
    events = [_event_for_span(span) for span in plan.spans]
    transport = FakeTransport(events=events)

    summary = apply_backfill(
        plan,
        ledger=_ledger(tmp_path),
        transport=transport,
        verify_timeout_seconds=0,
    )

    assert transport.uploads == []
    assert summary["uploaded_span_count"] == 0
    assert summary["already_present_span_count"] == 4
    assert summary["network_write_performed"] is False


def test_partial_remote_state_uploads_only_missing_deterministic_ids(tmp_path: Path) -> None:
    plan = build_backfill_plan(TRACE_ID, _mapped(), redactor=StepRedactor())
    transport = FakeTransport(events=[_event_for_span(plan.spans[0])])

    summary = apply_backfill(
        plan,
        ledger=_ledger(tmp_path),
        transport=transport,
        verify_timeout_seconds=0,
    )

    assert len(transport.uploads) == 1
    assert [span["span_id"] for span in transport.uploads[0]] == list(plan.span_ids[1:])
    assert summary["uploaded_span_count"] == 3
    assert summary["already_present_span_count"] == 1


def test_apply_refuses_out_of_order_or_nonprefix_deterministic_remote_ids(tmp_path: Path) -> None:
    plan = build_backfill_plan(TRACE_ID, _mapped(), redactor=StepRedactor())
    events = [_event_for_span(plan.spans[1]), _event_for_span(plan.spans[0])]
    transport = FakeTransport(events=events)

    with pytest.raises(TraceBackfillError, match="unexpected order"):
        apply_backfill(
            plan,
            ledger=_ledger(tmp_path),
            transport=transport,
            verify_timeout_seconds=0,
        )
    assert transport.uploads == []


def test_lost_upload_response_reconciles_by_remote_deterministic_ids(tmp_path: Path) -> None:
    plan = build_backfill_plan(TRACE_ID, _mapped(), redactor=StepRedactor())
    transport = FakeTransport(fail_after_accept=True)
    ledger = _ledger(tmp_path)

    summary = apply_backfill(plan, ledger=ledger, transport=transport, verify_timeout_seconds=0)

    assert summary["remote_verified"] is True
    assert summary["network_write_performed"] is True
    # The response was lost before the local uploaded counter advanced, but the
    # deterministic remote IDs prove that the entire batch arrived.
    assert summary["uploaded_span_count"] == 0
    assert json.loads(ledger.path.read_text())["traces"][TRACE_ID]["status"] == "verified"


@pytest.mark.parametrize("status", ["running", "pending"])
def test_apply_refuses_nonterminal_trace_without_upload_or_ledger_write(tmp_path: Path, status: str) -> None:
    plan = build_backfill_plan(TRACE_ID, _mapped(), redactor=StepRedactor())
    transport = FakeTransport(status=status)
    ledger = _ledger(tmp_path)

    with pytest.raises(TraceBackfillError, match="nonterminal"):
        apply_backfill(plan, ledger=ledger, transport=transport, verify_timeout_seconds=0)

    assert transport.uploads == []
    assert not ledger.path.exists()


@pytest.mark.parametrize("status", ["completed", "error", "cancelled"])
def test_apply_accepts_each_terminal_trace_status(tmp_path: Path, status: str) -> None:
    plan = build_backfill_plan(TRACE_ID, _mapped(), redactor=StepRedactor())
    transport = FakeTransport(status=status)

    summary = apply_backfill(
        plan,
        ledger=_ledger(tmp_path),
        transport=transport,
        verify_timeout_seconds=0,
    )

    assert summary["remote_status"] == status


def test_apply_fails_closed_if_deterministic_id_has_wrong_projected_kind(tmp_path: Path) -> None:
    plan = build_backfill_plan(TRACE_ID, _mapped(), redactor=StepRedactor())
    transport = FakeTransport(events=[{"id": plan.span_ids[0], "kind": "agent_message", "text": "wrong"}])

    with pytest.raises(TraceBackfillError, match="unexpected"):
        apply_backfill(
            plan,
            ledger=_ledger(tmp_path),
            transport=transport,
            verify_timeout_seconds=0,
        )
    assert transport.uploads == []


def test_apply_fails_closed_if_same_deterministic_id_has_different_visible_payload(tmp_path: Path) -> None:
    plan = build_backfill_plan(TRACE_ID, _mapped(), redactor=StepRedactor())
    events = [_event_for_span(span) for span in plan.spans]
    events[1]["tool_calls"][0]["arguments"] = {"command": "different historical command"}
    transport = FakeTransport(events=events)

    with pytest.raises(TraceBackfillError, match="visible payloads"):
        apply_backfill(
            plan,
            ledger=_ledger(tmp_path),
            transport=transport,
            verify_timeout_seconds=0,
        )

    assert transport.uploads == []


def test_verified_ledger_rejects_plan_drift_without_network_write(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    first = build_backfill_plan(TRACE_ID, _mapped(), redactor=StepRedactor())
    transport = FakeTransport()
    apply_backfill(first, ledger=ledger, transport=transport, verify_timeout_seconds=0)
    changed = build_backfill_plan(
        TRACE_ID,
        [
            KeyedStep("user:prompt", _user("changed prompt")),
            KeyedStep("response:one", _agent()),
            KeyedStep("observation:one", _tool()),
            KeyedStep("response:final", _done()),
        ],
        redactor=StepRedactor(),
    )
    upload_count = len(transport.uploads)

    with pytest.raises(TraceBackfillError, match="different ledger plan"):
        apply_backfill(changed, ledger=ledger, transport=transport, verify_timeout_seconds=0)

    assert len(transport.uploads) == upload_count


def test_ledger_rejects_insecure_existing_mode_and_symlink(tmp_path: Path) -> None:
    state = tmp_path / "private-state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    target = state / "target.json"
    target.write_text('{"schema_version":"1","traces":{}}')
    os.chmod(target, 0o600)
    insecure = state / "insecure.json"
    insecure.write_text('{"schema_version":"1","traces":{}}')
    os.chmod(insecure, 0o644)
    link = state / "link.json"
    link.symlink_to(target)

    with pytest.raises(TraceBackfillError, match="mode 0600"):
        BackfillLedger(insecure).load()
    with pytest.raises(TraceBackfillError, match="non-symlink"):
        BackfillLedger(link).load()


def test_http_transport_uses_bearer_auth_and_exact_api_routes_without_retry() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"status": "error", "events": []})
        body = json.loads(request.content)
        return httpx.Response(
            202,
            json={"status": "accepted", "count": len(body["telemetry"]), "sequence": 9},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = HTTPBackfillTransport(
        api_url="https://api.hud.ai/v2/",
        telemetry_url="https://telemetry.hud.ai/v3/api/",
        api_key="hud-secret-value",
        client=client,
    )
    plan = build_backfill_plan(TRACE_ID, _mapped(), redactor=StepRedactor())

    assert transport.fetch_events(TRACE_ID)["status"] == "error"
    assert transport.upload_spans(TRACE_ID, [plan.spans[0]])["count"] == 1
    transport.close()
    client.close()

    assert [str(request.url) for request in requests] == [
        f"https://api.hud.ai/v2/trace/{TRACE_ID}/events",
        f"https://telemetry.hud.ai/v3/api/trace/{TRACE_ID}/telemetry-upload",
    ]
    assert all(request.headers["Authorization"] == "Bearer hud-secret-value" for request in requests)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (
            {
                "api_url": "https://name:password@api.hud.ai",
                "telemetry_url": "https://telemetry.hud.ai/v3/api",
            },
            "HUD API URL",
        ),
        (
            {
                "api_url": "https://api.hud.ai",
                "telemetry_url": "file:///tmp/not-http",
            },
            "HUD telemetry URL",
        ),
        (
            {
                "api_url": "https://evil.example.test",
                "telemetry_url": "https://telemetry.hud.ai/v3/api",
            },
            "HUD API URL",
        ),
        (
            {
                "api_url": "http://api.hud.ai",
                "telemetry_url": "https://telemetry.hud.ai/v3/api",
            },
            "HUD API URL",
        ),
        (
            {
                "api_url": "https://api.hud.ai:443",
                "telemetry_url": "https://telemetry.hud.ai/v3/api",
            },
            "HUD API URL",
        ),
        (
            {
                "api_url": "https://api.beta.hud.ai",
                "telemetry_url": "https://evil.example.test/v3/api",
            },
            "HUD telemetry URL",
        ),
    ],
)
def test_http_transport_rejects_credentialed_or_non_http_roots(
    kwargs: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(TraceBackfillError, match=message):
        HTTPBackfillTransport(api_key="value", **kwargs)


def test_http_transport_accepts_pinned_beta_api_with_pinned_telemetry() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})))
    transport = HTTPBackfillTransport(
        api_url="https://api.beta.hud.ai",
        telemetry_url="https://telemetry.hud.ai/v3/api",
        api_key="value",
        client=client,
    )
    transport.close()
    client.close()


def test_http_transport_rejects_redirect_following_client_before_credentials_are_used() -> None:
    client = httpx.Client(follow_redirects=True)
    with pytest.raises(TraceBackfillError, match="may not follow redirects"):
        HTTPBackfillTransport(
            api_url="https://api.hud.ai",
            telemetry_url="https://telemetry.hud.ai/v3/api",
            api_key="value",
            client=client,
        )
    client.close()


def test_cli_defaults_to_offline_dry_run_and_emits_counts_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "trajectory.json"
    source.write_text("not opened by the fake mapper")
    monkeypatch.setattr("cybergym_hud.trace_backfill._load_mapper", lambda _reference: lambda _path: _mapped())

    result = main(
        [
            "--trace-id",
            TRACE_ID,
            "--source",
            str(source),
            "--mapper",
            "future.mapper:map_trajectory",
            "--dry-run",
            "--expect-agent",
            "2",
            "--expect-tool",
            "1",
            "--expect-user",
            "1",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    body = json.loads(output)
    assert body["mode"] == "dry-run"
    assert body["network_write_performed"] is False
    assert body["event_counts"] == {"agent_message": 2, "tool_call": 1, "user_message": 1}
    assert "repair the target" not in output


def test_cli_count_gate_fails_before_any_network_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "trajectory.json"
    source.write_text("fixture")
    monkeypatch.setattr("cybergym_hud.trace_backfill._load_mapper", lambda _reference: lambda _path: _mapped())

    result = main(
        [
            "--trace-id",
            TRACE_ID,
            "--source",
            str(source),
            "--mapper",
            "future.mapper:map_trajectory",
            "--expect-agent",
            "14",
        ]
    )

    assert result == 2
    error = json.loads(capsys.readouterr().err)
    assert "operator expectations" in error["error"]


def test_cli_apply_requires_all_three_exact_count_gates_before_mapping_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "trajectory.json"
    source.write_text("fixture")
    mapper_loaded = False

    def forbidden_mapper(_reference: str) -> Any:
        nonlocal mapper_loaded
        mapper_loaded = True
        raise AssertionError("mapper must not load")

    monkeypatch.setattr("cybergym_hud.trace_backfill._load_mapper", forbidden_mapper)
    result = main(
        [
            "--trace-id",
            TRACE_ID,
            "--source",
            str(source),
            "--mapper",
            "future.mapper:map_trajectory",
            "--apply",
            "--expect-user",
            "1",
            "--expect-agent",
            "14",
        ]
    )

    assert result == 2
    assert mapper_loaded is False
    assert "requires --expect-user" in json.loads(capsys.readouterr().err)["error"]

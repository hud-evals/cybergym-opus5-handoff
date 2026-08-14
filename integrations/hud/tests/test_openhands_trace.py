from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest
from hud.agents.types import AgentStep, ToolStep
from mcp.types import TextContent

from cybergym_hud.contract import OG_PROMPT
from cybergym_hud.openhands_trace import (
    DecodedOpenHandsEvent,
    OpenHandsEventProjector,
    TraceImportError,
    import_openhands_trace,
    map_openhands_receipt,
    validate_remote_trace_projection,
)
from cybergym_hud.receipt import NativeReceipt, NativeRunProfile
from cybergym_hud.trace_backfill import StepRedactor, build_backfill_plan


def _response(response_id: str, calls: list[tuple[str, str, dict]], *, reasoning: str | None = None) -> dict:
    message: dict = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            for call_id, name, arguments in calls
        ],
        "function_call": None,
    }
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "id": response_id,
        "created": 1,
        "model": "fixture-model",
        "object": "chat.completion",
        "choices": [{"finish_reason": "tool_calls", "index": 0, "message": message}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 80},
            "completion_tokens_details": {"reasoning_tokens": 10},
        },
    }


def _metadata(response: dict, call_id: str) -> dict:
    calls = response["choices"][0]["message"]["tool_calls"]
    function = next(call["function"]["name"] for call in calls if call["id"] == call_id)
    return {
        "function_name": function,
        "tool_call_id": call_id,
        "model_response": response,
        "total_calls_in_response": len(calls),
    }


def _action(event_id: int, action: str, response: dict, call_id: str, *, thought: str = "") -> dict:
    return {
        "id": event_id,
        "timestamp": f"2026-01-02T03:04:{event_id:02d}.000001",
        "source": "agent",
        "message": "fixture action",
        "action": action,
        "tool_call_metadata": _metadata(response, call_id),
        "args": {"thought": thought},
    }


def _observation(event_id: int, action_id: int, action: str, response: dict, call_id: str, content: str) -> dict:
    return {
        "id": event_id,
        "timestamp": f"2026-01-02T03:04:{event_id:02d}.000001",
        "source": "agent",
        "message": "fixture observation",
        "cause": action_id,
        "observation": action,
        "tool_call_metadata": _metadata(response, call_id),
        "content": content,
        "success": True,
    }


def _finish(event_id: int, text: str) -> dict:
    response = _response("response-finish", [("call-finish", "finish", {"final_thought": text})])
    return {
        "id": event_id,
        "timestamp": f"2026-01-02T03:05:{event_id:02d}.000001",
        "source": "agent",
        "message": "fixture finish",
        "action": "finish",
        "tool_call_metadata": _metadata(response, "call-finish"),
        "args": {"final_thought": text, "task_completed": "true", "thought": ""},
    }


def _parallel_events(secret: str = "fixture-secret-value") -> list[dict]:  # noqa: S107
    response = _response(
        "response-parallel",
        [
            ("call-one", "execute_bash", {"command": "inspect", "api_key": secret}),
            ("call-two", "web_read", {"url": f"http://controller/{secret}"}),
        ],
        reasoning=f"inspect carefully with {secret}",
    )
    return [
        _action(2, "run", response, "call-one"),
        _observation(3, 2, "run", response, "call-one", f"first result {secret}"),
        _action(4, "browse", response, "call-two"),
        _observation(5, 4, "browse", response, "call-two", "flag{fixture-not-a-real-flag}"),
    ]


def test_projects_parallel_response_once_in_provider_order_and_finish() -> None:
    raw = [
        {
            "id": 0,
            "timestamp": "2026-01-02T03:04:00.000001",
            "source": "user",
            "message": "fixture",
            "action": "message",
            "args": {"content": OG_PROMPT},
        },
        {
            "id": 1,
            "timestamp": "2026-01-02T03:04:01.000001",
            "source": "environment",
            "message": "",
            "observation": "agent_state_changed",
            "content": "",
            "extras": {"agent_state": "running"},
        },
        *_parallel_events(),
        _finish(6, "Finished fixture task."),
    ]
    projected = OpenHandsEventProjector().project(raw, final=True, skip_initial_user_prompt=OG_PROMPT)

    assert [item.key for item in projected] == [
        "response:response-parallel",
        "tool:call-one",
        "tool:call-two",
        "response:response-finish",
    ]
    assert isinstance(projected[0].step, AgentStep)
    assert [call.id for call in projected[0].step.tool_calls] == ["call-one", "call-two"]
    assert projected[0].step.reasoning == "inspect carefully with fixture-secret-value"
    assert projected[0].step.started_at.endswith("Z")
    assert all(isinstance(item.step, ToolStep) for item in projected[1:3])
    assert projected[-1].step.done is True
    assert projected[-1].step.content == "Finished fixture task."


def test_projects_user_messages_as_typed_text_content() -> None:
    raw = {
        "id": 7,
        "timestamp": "2026-01-02T03:04:07+00:00",
        "source": "user",
        "message": "fixture",
        "action": "message",
        "args": {"content": "continue"},
    }
    projected = OpenHandsEventProjector().project([raw], final=False)
    assert projected[0].step.source == "user"
    assert isinstance(projected[0].step.messages[0].content, TextContent)
    assert projected[0].step.messages[0].content.text == "continue"
    assert projected[0].step.ended_at == projected[0].step.started_at


def test_projects_sanitized_decoded_events_without_raw_redecode() -> None:
    projector = OpenHandsEventProjector()
    raw = _parallel_events()
    decoded = tuple(projector.decode(event, origin="event_store") for event in raw)

    assert all(isinstance(event, DecodedOpenHandsEvent) for event in decoded)
    assert projector.project(decoded, final=True, origin="event_store") == projector.project(
        raw,
        final=True,
        origin="event_store",
    )


def test_rejects_mixed_raw_and_decoded_events() -> None:
    projector = OpenHandsEventProjector()
    raw = _parallel_events()
    decoded = projector.decode(raw[0], origin="event_store")

    with pytest.raises(TraceImportError, match="mixes raw and decoded"):
        projector.project([decoded, raw[1]], final=False, origin="event_store")


def test_live_prompt_suppression_withholds_empty_prefix() -> None:
    projector = OpenHandsEventProjector()

    assert projector.project([], final=False, skip_initial_user_prompt=OG_PROMPT) == ()
    with pytest.raises(TraceImportError, match="has no outer user prompt"):
        projector.project([], final=True, skip_initial_user_prompt=OG_PROMPT)


def test_live_prefix_emits_agent_and_only_contiguous_tool_results() -> None:
    events = _parallel_events()
    projected = OpenHandsEventProjector().project(events[:3], final=False)
    assert [item.key for item in projected] == ["response:response-parallel", "tool:call-one"]

    with pytest.raises(TraceImportError, match="missing tool result"):
        OpenHandsEventProjector().project(events[:3], final=True)


def test_redacts_exact_values_sensitive_fields_and_secret_patterns_recursively() -> None:
    secret = "fixture-secret-value"  # noqa: S105
    projected = OpenHandsEventProjector(redactions=[secret]).project(
        [*_parallel_events(secret), _finish(6, f"done with sk-proj-abcdefghijklmnopqrstuvwxyz and {secret}")],
        final=True,
    )
    encoded = json.dumps([item.step.model_dump(mode="json") for item in projected])
    assert secret not in encoded
    assert "flag{fixture-not-a-real-flag}" not in encoded
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz" not in encoded
    assert encoded.count("[REDACTED]") >= 5


def test_oversized_exported_fields_have_deterministic_digest_markers() -> None:
    huge = "x" * (2 * 1024 * 1024)
    response = _response("response-huge", [("call-huge", "execute_bash", {"command": huge})])
    events = [
        _action(2, "run", response, "call-huge"),
        _observation(3, 2, "run", response, "call-huge", huge),
    ]
    first = OpenHandsEventProjector().project(events, final=True)
    second = OpenHandsEventProjector().project(events, final=True)
    encoded = json.dumps([item.step.model_dump(mode="json") for item in first])
    # The bounded call is present once on AgentStep and once on ToolStep;
    # the bounded observation is the third marker.
    assert len(encoded.encode()) < 900 * 1024
    assert len(re.findall(r"HUD_TRUNCATED original_bytes=\d+ sha256=[0-9a-f]{64}", encoded)) == 3
    assert encoded == json.dumps([item.step.model_dump(mode="json") for item in second])


def test_divergent_duplicate_provider_response_fails_closed() -> None:
    events = _parallel_events()
    events[1] = deepcopy(events[1])
    events[1]["tool_call_metadata"]["model_response"]["model"] = "diverged"
    with pytest.raises(TraceImportError, match="divergent provider payloads"):
        OpenHandsEventProjector().project(events, final=True)


def test_duplicate_provider_tool_call_ids_fail_closed() -> None:
    response = _response(
        "response-duplicate-call",
        [
            ("same-call", "execute_bash", {"command": "one"}),
            ("same-call", "web_read", {"url": "http://fixture"}),
        ],
    )
    with pytest.raises(TraceImportError, match="repeats a provider tool-call id"):
        OpenHandsEventProjector().decode(
            _action(2, "run", response, "same-call"),
            origin="fixture",
        )


def test_finish_provider_call_requires_openhands_finish_action() -> None:
    response = _response("response-false-finish", [("call-finish", "finish", {"final_thought": "done"})])
    with pytest.raises(TraceImportError, match="exact OpenHands finish"):
        OpenHandsEventProjector().project(
            [_action(2, "run", response, "call-finish")],
            final=True,
        )


def test_openhands_finish_action_requires_finish_provider_call() -> None:
    response = _response("response-false-finish", [("call-run", "execute_bash", {"command": "true"})])
    event = _action(2, "finish", response, "call-run")
    event["args"] = {"final_thought": "done"}
    with pytest.raises(TraceImportError, match="mixes finish"):
        OpenHandsEventProjector().project([event], final=True)


def _profile() -> NativeRunProfile:
    return NativeRunProfile(
        budget_profile="custom",
        model="fixture-model",
        max_iter=10,
        timeout_seconds=30,
        max_output_tokens=100,
        temperature=0,
        top_p=1,
        base_url_mode="provider-default",
    )


def test_posthoc_import_verifies_prompt_redacts_task_secrets_and_digests(tmp_path: Path) -> None:
    agent_id = "a" * 32
    checksum = "b" * 64
    server = "http://127.0.0.1:8666"
    args = {
        "task": {"task_id": "arvo:10013", "agent_id": agent_id, "checksum": checksum, "server": server},
        "agent_args": {"llm": {"api_key": None}},
    }
    (tmp_path / "args.json").write_text(json.dumps(args), encoding="utf-8")
    events = [
        {
            "id": 0,
            "timestamp": "2026-01-02T03:04:00",
            "source": "user",
            "message": "fixture",
            "action": "message",
            "args": {"content": OG_PROMPT},
        },
        *_parallel_events(checksum),
        _finish(6, f"submitted through {server} for {agent_id}"),
    ]
    (tmp_path / "trajectory").write_text(json.dumps(events), encoding="utf-8")
    receipt = NativeReceipt(
        status="completed",
        task_id="arvo:10013",
        server=server,
        run_profile=_profile(),
        agent_id=agent_id,
        upstream_returned_agent_id=agent_id,
        log_dir=str(tmp_path),
    )

    imported = import_openhands_trace(receipt)
    encoded = json.dumps([item.step.model_dump(mode="json") for item in imported.steps])
    assert imported.metadata["projected_step_count"] == 4
    assert imported.metadata["agent_step_count"] == 2
    assert imported.metadata["tool_step_count"] == 2
    assert len(imported.metadata["projected_steps_sha256"]) == 64
    assert len(imported.metadata["projected_events_sha256"]) == 64
    assert all(value not in encoded for value in (agent_id, checksum, server))
    assert not any(item.step.source == "user" for item in imported.steps)

    sidecar = tmp_path / "receipt.json"
    sidecar.write_text(receipt.model_dump_json(), encoding="utf-8")
    backfill = map_openhands_receipt(sidecar)
    assert backfill[0].key == "user:0"
    assert backfill[0].step.messages[0].content.text == OG_PROMPT
    assert [item.key for item in backfill[1:]] == [item.key for item in imported.steps]

    remote_events = []
    for item in imported.steps:
        step = item.step
        if isinstance(step, AgentStep):
            remote_events.append(
                {
                    "kind": "agent_message",
                    "text": step.content,
                    "reasoning": step.reasoning,
                    "tool_calls": [
                        {
                            "tool_call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments or {},
                        }
                        for call in step.tool_calls
                    ],
                }
            )
        elif isinstance(step, ToolStep):
            assert step.call is not None and step.result is not None
            result_text = "\n".join(
                content.text for content in step.result.content if isinstance(content, TextContent)
            )
            remote_events.append(
                {
                    "kind": "tool_call",
                    "tool_name": step.call.name,
                    "arguments": step.call.arguments or {},
                    "result_text": result_text,
                    "result_data": None,
                    "error": None,
                }
            )
    remote_events.append(
        {
            "kind": "raw",
            "attributes": {"openhands_trace_import": imported.metadata},
        }
    )
    assert validate_remote_trace_projection(remote_events) == imported.metadata

    corrupted = deepcopy(remote_events)
    corrupted[0]["text"] = "same counts, different assistant content"
    with pytest.raises(TraceImportError, match="conversation content disagrees"):
        validate_remote_trace_projection(corrupted)


def test_projector_output_builds_timestamp_complete_1_14_22_backfill_plan(tmp_path: Path) -> None:
    agent_id = "e" * 32
    checksum = "f" * 64
    server = "http://127.0.0.1:8666"
    task_id = "arvo:10252"
    events: list[dict] = [
        {
            "id": 0,
            "timestamp": "2026-01-02T03:04:00",
            "source": "user",
            "message": "fixture",
            "action": "message",
            "args": {"content": OG_PROMPT},
        }
    ]
    event_id = 1
    # Thirteen ordinary assistant turns: nine parallel pairs plus four
    # single calls = 22 tool results, followed by one finish turn.
    for group in range(13):
        call_count = 2 if group < 9 else 1
        calls = [
            (f"call-{group}-{index}", "execute_bash", {"command": f"fixture-{group}-{index}"})
            for index in range(call_count)
        ]
        response = _response(f"response-{group}", calls)
        for call_id, _name, _arguments in calls:
            events.append(_action(event_id, "run", response, call_id))
            action_id = event_id
            event_id += 1
            events.append(_observation(event_id, action_id, "run", response, call_id, "ok"))
            event_id += 1
    events.append(_finish(event_id, "Finished fixture task."))
    (tmp_path / "args.json").write_text(
        json.dumps(
            {
                "task": {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "checksum": checksum,
                    "server": server,
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "trajectory").write_text(json.dumps(events), encoding="utf-8")
    receipt = NativeReceipt(
        status="completed",
        task_id=task_id,
        server=server,
        run_profile=_profile(),
        agent_id=agent_id,
        upstream_returned_agent_id=agent_id,
        log_dir=str(tmp_path),
    )
    sidecar = tmp_path / "receipt.json"
    sidecar.write_text(receipt.model_dump_json(), encoding="utf-8")

    plan = build_backfill_plan(
        str(UUID("12345678-1234-5678-1234-567812345678")),
        map_openhands_receipt(sidecar),
        redactor=StepRedactor(),
    )
    assert plan.event_counts == {"user_message": 1, "agent_message": 14, "tool_call": 22}


def test_error_receipt_preserves_validated_partial_transcript(tmp_path: Path) -> None:
    agent_id = "c" * 32
    server = "http://127.0.0.1:8666"
    (tmp_path / "args.json").write_text(
        json.dumps({"task": {"task_id": "arvo:10055", "agent_id": agent_id, "checksum": "d" * 64, "server": server}}),
        encoding="utf-8",
    )
    events = [
        {
            "id": 0,
            "timestamp": "2026-01-02T03:04:00",
            "source": "user",
            "message": "fixture",
            "action": "message",
            "args": {"content": OG_PROMPT},
        },
        *_parallel_events(),
    ]
    (tmp_path / "trajectory").write_text(json.dumps(events[:-1]), encoding="utf-8")
    receipt = NativeReceipt(
        status="error",
        task_id="arvo:10055",
        server=server,
        run_profile=_profile(),
        agent_id=agent_id,
        upstream_returned_agent_id=agent_id,
        log_dir=str(tmp_path),
        error="controller interrupted",
    )
    imported = import_openhands_trace(receipt)
    assert imported.metadata["status"] == "partial_error"
    assert [item.key for item in imported.steps] == ["response:response-parallel", "tool:call-one"]


def test_remote_projection_gate_requires_visible_agent_tool_shape_and_receipt() -> None:
    receipt = {
        "schema_version": "1",
        "status": "completed",
        "projected_step_count": 2,
        "agent_step_count": 1,
        "tool_step_count": 1,
        "user_step_count": 0,
        "source_has_tool_actions": True,
        "projected_steps_sha256": "a" * 64,
    }
    events = [
        {"kind": "agent_message", "text": None, "reasoning": None, "tool_calls": [{"id": "call-1"}]},
        {"kind": "tool_call", "name": "execute_bash"},
        {
            "kind": "raw",
            "attributes": {"hud.payload": {"extra": {"openhands_trace_import": receipt}}},
        },
    ]
    assert validate_remote_trace_projection(events) == receipt
    with pytest.raises(TraceImportError, match="lost all source tool calls"):
        validate_remote_trace_projection([events[0], events[2]])

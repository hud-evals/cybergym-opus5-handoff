"""Faithful selected, secret-safe projection of OpenHands events into HUD steps.

Only model-visible messages, tool requests, and tool results are selected; raw
browser DOM/screenshot metadata is intentionally excluded. Exported text and
argument fields over 256 KiB are deterministically truncated with their source
byte count and SHA-256 digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from hud.agents.types import AgentStep, ToolStep, Usage
from hud.types import MCPToolCall, MCPToolResult, Step
from mcp.types import PromptMessage, TextContent

from .contract import OG_PROMPT
from .receipt import NativeReceipt

REDACTION = "[REDACTED]"
_MAX_TRACE_BYTES = 128 * 1024 * 1024
_MAX_EXPORTED_FIELD_BYTES = 256 * 1024
_RUNTIME_SECRET_NAME = re.compile(
    r"(?:API_KEY|TOKEN|PASSWORD|SECRET|SECRET_ACCESS_KEY|CREDENTIALS|PRIVATE_KEY|AUTH|COOKIE|SESSION|CHECKSUM)$",
    re.IGNORECASE,
)
_SENSITIVE_FIELD = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|password|secret|checksum|"
    r"agent[_-]?id|private[_-]?key|github[_-]?pat|session[_-]?id|"
    r"(?:^|[_-])(?:token|cookies?|auth|credentials?|session(?:[_-]?id)?)$)",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT_NAME = (
    r"[A-Za-z0-9_-]{0,64}(?:API[_-]?KEY|TOKEN|COOKIE|AUTH|PASSWORD|SECRET|CHECKSUM|AGENT[_-]?ID|"
    r"PRIVATE[_-]?KEY|CREDENTIALS?|SESSION(?:[_-]?ID)?)|github[_-]?pat"
)
_TEXT_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(?:flag|ctf|picoctf|htb)\{[^}\r\n]{1,512}\}"), REDACTION),
    (
        re.compile(
            r"(?i)\b(?:sk-(?:proj-|ant-)?|sess-|github_pat_|gh[pousr]_|glpat-|hf_|xox[a-z]-)"
            r"[A-Za-z0-9._-]{8,}\b"
        ),
        REDACTION,
    ),
    (re.compile(r"\b(?:AIza[0-9A-Za-z_-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b"), REDACTION),
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), rf"\1{REDACTION}"),
    (
        re.compile(r"(?i)\b(Authorization\s*:\s*Basic\s+)[A-Za-z0-9+/=]{8,}"),
        rf"\1{REDACTION}",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        REDACTION,
    ),
    (re.compile(r"(?im)^(\s*(?:Cookie|Set-Cookie)\s*:\s*)[^\r\n]+"), rf"\1{REDACTION}"),
    (
        re.compile(
            rf"(?i)(?<![A-Za-z0-9_-])"
            rf"(['\"]?(?:{_SENSITIVE_ASSIGNMENT_NAME})['\"]?(?![A-Za-z0-9_-])\s*[:=]\s*)"
            rf"(['\"])[^'\"\r\n]{{1,2048}}\2"
        ),
        rf"\1\2{REDACTION}\2",
    ),
    (
        re.compile(
            rf"(?i)(?<![A-Za-z0-9_-])"
            rf"(['\"]?(?:{_SENSITIVE_ASSIGNMENT_NAME})['\"]?(?![A-Za-z0-9_-])\s*[:=]\s*)"
            rf"[^\s,;}}\]]+"
        ),
        rf"\1{REDACTION}",
    ),
)


class TraceImportError(RuntimeError):
    """The saved trajectory cannot be projected without guessing or leaking data."""


@dataclass(frozen=True, slots=True)
class _Call:
    id: str
    name: str
    arguments: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class DecodedOpenHandsEvent:
    """A selected, redacted event shape; raw OpenHands metadata is never retained."""

    event_id: str
    timestamp: str | None
    kind: Literal["user", "agent_text", "action", "observation", "skip"]
    text: str | None = None
    action_name: str | None = None
    cause: str | None = None
    call_id: str | None = None
    response_id: str | None = None
    response_fingerprint: str | None = None
    calls: tuple[_Call, ...] = ()
    model: str | None = None
    reasoning: str | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    total_calls: int | None = None
    success: bool | None = None


@dataclass(frozen=True, slots=True)
class ProjectedStep:
    key: str
    step: Step


@dataclass(frozen=True, slots=True)
class TraceImportResult:
    steps: tuple[ProjectedStep, ...]
    metadata: dict[str, Any]


class _Redactor:
    def __init__(self, values: Iterable[str]) -> None:
        # Reject tiny exact literals: a misconfigured TOKEN=x must not corrupt
        # every occurrence of that character in the transcript.
        self.values = tuple(sorted({value for value in values if len(value) >= 8}, key=len, reverse=True))

    def text(self, value: str | None) -> str | None:
        value = self._raw_text(value)
        return None if value is None else _bounded_text(value)

    def _raw_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        for secret in self.values:
            value = value.replace(secret, REDACTION)
        for pattern, replacement in _TEXT_REDACTIONS:
            value = pattern.sub(replacement, value)
        return value

    def value(self, value: Any, *, key: str | None = None) -> Any:
        if key is not None and _SENSITIVE_FIELD.search(key):
            return REDACTION
        if isinstance(value, str):
            return self._raw_text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            rendered: dict[str, Any] = {}
            for item_key, item in value.items():
                raw_key = str(item_key)
                redacted_key = self._raw_text(raw_key)
                if redacted_key is None:
                    raise TraceImportError("tool argument key unexpectedly disappeared")
                if redacted_key in rendered:
                    raise TraceImportError("tool argument key redaction collapsed distinct keys")
                rendered[redacted_key] = self.value(item, key=raw_key)
            return rendered
        if value is None or isinstance(value, (bool, int, float)):
            return value
        raise TraceImportError(f"unsupported value in tool arguments: {type(value).__name__}")

    def arguments(self, value: Any) -> dict[str, Any] | None:
        redacted = self.value(value)
        if redacted is None:
            return None
        if not isinstance(redacted, dict):
            raise TraceImportError("provider tool arguments must be a JSON object")
        try:
            canonical = json.dumps(redacted, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise TraceImportError("redacted tool arguments are not JSON serializable") from exc
        if len(canonical.encode()) <= _MAX_EXPORTED_FIELD_BYTES:
            return redacted
        # Keep the bounded representation object-shaped so HUD's v1 control-
        # plane schema and the historical backfill verifier can still project
        # it as a tool call instead of degrading the whole span to ``raw``.
        return {"__hud_truncated_json__": _bounded_text(canonical)}


def _bounded_text(value: str) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= _MAX_EXPORTED_FIELD_BYTES:
        return value
    digest = hashlib.sha256(raw).hexdigest()
    marker = f"\n[HUD_TRUNCATED original_bytes={len(raw)} sha256={digest}]"
    budget = _MAX_EXPORTED_FIELD_BYTES - len(marker.encode())
    prefix = raw[:budget]
    while True:
        try:
            rendered = prefix.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            prefix = prefix[: exc.start]
    return rendered + marker


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TraceImportError(f"{label} must be an object")
    return value


def _string(value: object, *, label: str, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value):
        raise TraceImportError(f"{label} must be a nonempty string")
    return value


def _json_fingerprint(value: object) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    except (TypeError, ValueError) as exc:
        raise TraceImportError("model response is not JSON serializable") from exc
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value: object, *, label: str) -> str | None:
    rendered = _string(value, label=label, required=False)
    if rendered is None:
        return None
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TraceImportError(f"{label} is not ISO-8601") from exc
    # Pinned OpenHands writes naive UTC timestamps. Make that source contract
    # explicit so downstream importers never interpret them as local time.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat().replace("+00:00", "Z")


def _projection_timestamp(value: str | None, *, key: str, field: str) -> datetime:
    if value is None:
        raise TraceImportError(f"projected step {key} has no {field} timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TraceImportError(f"projected step {key} has invalid {field} timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TraceImportError(f"projected step {key} has a naive {field} timestamp")
    return parsed.astimezone(UTC)


def _normalize_projection_timestamps(steps: Sequence[ProjectedStep]) -> tuple[ProjectedStep, ...]:
    """Make control-plane sorting preserve exact semantic projection order."""

    normalized: list[ProjectedStep] = []
    previous_start: datetime | None = None
    for item in steps:
        original_start = _projection_timestamp(
            item.step.started_at,
            key=item.key,
            field="start",
        )
        original_end = _projection_timestamp(
            item.step.ended_at,
            key=item.key,
            field="end",
        )
        duration = max(original_end - original_start, timedelta(0))
        start = original_start
        if previous_start is not None and start <= previous_start:
            start = previous_start + timedelta(microseconds=1)
        end = start + duration
        normalized.append(
            ProjectedStep(
                item.key,
                item.step.model_copy(
                    update={
                        "started_at": start.isoformat().replace("+00:00", "Z"),
                        "ended_at": end.isoformat().replace("+00:00", "Z"),
                    }
                ),
            )
        )
        previous_start = start
    return tuple(normalized)


class OpenHandsEventProjector:
    """Decode and project append-only OpenHands trajectory event objects."""

    def __init__(self, *, redactions: Iterable[str] = ()) -> None:
        self._redactor = _Redactor(redactions)

    def decode(self, raw: object, *, origin: str) -> DecodedOpenHandsEvent:
        event = _mapping(raw, label=f"{origin} event")
        event_id = str(event.get("id", ""))
        if not event_id:
            raise TraceImportError(f"{origin} event has no id")
        timestamp = _timestamp(event.get("timestamp"), label=f"{origin} event {event_id} timestamp")
        source = _string(event.get("source"), label=f"{origin} event {event_id} source")
        action = event.get("action")
        observation = event.get("observation")

        if action in {"recall", "agent_state_changed"} or observation in {"recall", "agent_state_changed"}:
            return DecodedOpenHandsEvent(event_id, timestamp, "skip")
        if source == "environment" and action is None and observation is None:
            return DecodedOpenHandsEvent(event_id, timestamp, "skip")
        if source == "user" and action == "message":
            args = _mapping(event.get("args"), label=f"{origin} user event {event_id} args")
            text = _string(args.get("content"), label=f"{origin} user event {event_id} content")
            return DecodedOpenHandsEvent(event_id, timestamp, "user", text=self._redactor.text(text))
        if source == "agent" and action == "message" and event.get("tool_call_metadata") is None:
            args = _mapping(event.get("args"), label=f"{origin} agent message {event_id} args")
            text = _string(args.get("content"), label=f"{origin} agent message {event_id} content")
            if not text.strip():
                raise TraceImportError(f"{origin} agent message {event_id} content is blank")
            return DecodedOpenHandsEvent(
                event_id,
                timestamp,
                "agent_text",
                text=self._redactor.text(text),
            )

        metadata = _mapping(event.get("tool_call_metadata"), label=f"{origin} event {event_id} metadata")
        call_id = _string(metadata.get("tool_call_id"), label=f"{origin} event {event_id} call id")
        response = _mapping(metadata.get("model_response"), label=f"{origin} event {event_id} response")
        response_id = _string(response.get("id"), label=f"{origin} event {event_id} response id")
        total_calls = metadata.get("total_calls_in_response")
        if not isinstance(total_calls, int) or total_calls < 1:
            raise TraceImportError(f"{origin} event {event_id} has invalid response call count")
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise TraceImportError(f"{origin} event {event_id} response must have exactly one choice")
        choice = _mapping(choices[0], label=f"{origin} event {event_id} response choice")
        message = _mapping(choice.get("message"), label=f"{origin} event {event_id} response message")
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list) or len(raw_calls) != total_calls:
            raise TraceImportError(f"{origin} event {event_id} provider tool-call count disagrees with metadata")
        calls: list[_Call] = []
        for index, raw_call in enumerate(raw_calls):
            provider_call = _mapping(raw_call, label=f"{origin} response {response_id} call {index}")
            provider_id = _string(provider_call.get("id"), label=f"{origin} response {response_id} call id")
            function = _mapping(
                provider_call.get("function"), label=f"{origin} response {response_id} call {provider_id} function"
            )
            name = _string(function.get("name"), label=f"{origin} response {response_id} call {provider_id} name")
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise TraceImportError(
                        f"{origin} response {response_id} call {provider_id} arguments are not JSON"
                    ) from exc
            arguments = self._redactor.arguments(arguments)
            calls.append(_Call(provider_id, name, arguments))

        if len({call.id for call in calls}) != len(calls):
            raise TraceImportError(f"{origin} response {response_id} repeats a provider tool-call id")

        if call_id not in {call.id for call in calls}:
            raise TraceImportError(f"{origin} event {event_id} call id is absent from provider response")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise TraceImportError(f"{origin} response {response_id} content is not text")
        reasoning_values = [message.get(key) for key in ("reasoning_content", "reasoning", "thinking")]
        reasoning_parts = [part for part in reasoning_values if isinstance(part, str) and part]
        if any(part is not None and not isinstance(part, str) for part in reasoning_values):
            raise TraceImportError(f"{origin} response {response_id} reasoning is not text")
        usage = self._usage(response.get("usage"), origin=origin, response_id=response_id)
        model = response.get("model") if isinstance(response.get("model"), str) else None
        finish_reason = choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None

        if source == "agent" and isinstance(action, str):
            args = _mapping(event.get("args", {}), label=f"{origin} action {event_id} args")
            if action == "finish":
                final_text = args.get("final_thought") or args.get("thought") or content or event.get("message")
                final_text = _string(final_text, label=f"{origin} finish event {event_id} content")
                content = final_text
            action_reasoning = args.get("thought")
            if action_reasoning:
                if not isinstance(action_reasoning, str):
                    raise TraceImportError(f"{origin} action {event_id} thought is not text")
                if action_reasoning not in reasoning_parts:
                    reasoning_parts.append(action_reasoning)
            return DecodedOpenHandsEvent(
                event_id=event_id,
                timestamp=timestamp,
                kind="action",
                text=self._redactor.text(content),
                action_name=action,
                call_id=call_id,
                response_id=response_id,
                response_fingerprint=_json_fingerprint(response),
                calls=tuple(calls),
                model=self._redactor.text(model),
                reasoning=self._redactor.text("\n\n".join(reasoning_parts) or None),
                usage=usage,
                finish_reason=finish_reason,
                total_calls=total_calls,
            )
        if source == "agent" and isinstance(observation, str):
            content = event.get("content")
            if not isinstance(content, str):
                raise TraceImportError(f"{origin} observation {event_id} content is not text")
            cause = event.get("cause")
            if not isinstance(cause, (str, int)):
                raise TraceImportError(f"{origin} observation {event_id} has no cause")
            success = event.get("success")
            if success is not None and not isinstance(success, bool):
                raise TraceImportError(f"{origin} observation {event_id} success is not boolean")
            return DecodedOpenHandsEvent(
                event_id=event_id,
                timestamp=timestamp,
                kind="observation",
                text=self._redactor.text(content),
                action_name=observation,
                cause=str(cause),
                call_id=call_id,
                response_id=response_id,
                response_fingerprint=_json_fingerprint(response),
                calls=tuple(calls),
                total_calls=total_calls,
                success=success,
            )
        raise TraceImportError(f"unsupported {origin} event {event_id} shape")

    @staticmethod
    def _usage(raw: object, *, origin: str, response_id: str) -> Usage | None:
        if raw is None:
            return None
        usage = _mapping(raw, label=f"{origin} response {response_id} usage")
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        details = usage.get("prompt_tokens_details") or {}
        details = _mapping(details, label=f"{origin} response {response_id} prompt-token details")
        cached = details.get("cached_tokens")
        for label, value in (("prompt", prompt), ("completion", completion), ("cached", cached)):
            if value is not None and (not isinstance(value, int) or value < 0):
                raise TraceImportError(f"{origin} response {response_id} has invalid {label} tokens")
        return Usage(prompt_tokens=prompt, completion_tokens=completion, cached_tokens=cached)

    def project(
        self,
        events: Iterable[object],
        *,
        final: bool,
        origin: str = "trajectory",
        skip_initial_user_prompt: str | None = None,
    ) -> tuple[ProjectedStep, ...]:
        event_list = list(events)
        decoded_inputs = [isinstance(event, DecodedOpenHandsEvent) for event in event_list]
        if any(decoded_inputs) and not all(decoded_inputs):
            raise TraceImportError(f"{origin} mixes raw and decoded OpenHands events")
        decoded = event_list if all(decoded_inputs) else [self.decode(raw, origin=origin) for raw in event_list]
        seen_event_ids: set[str] = set()
        for event in decoded:
            if event.event_id in seen_event_ids:
                raise TraceImportError(f"duplicate {origin} event id {event.event_id}")
            seen_event_ids.add(event.event_id)

        output: list[ProjectedStep] = []
        skipped_user_id: str | None = None
        if skip_initial_user_prompt is not None:
            first_visible = next((event for event in decoded if event.kind != "skip"), None)
            if first_visible is None:
                if final:
                    raise TraceImportError(f"{origin} has no outer user prompt")
                return ()
            if first_visible.kind != "user" or first_visible.text != skip_initial_user_prompt:
                raise TraceImportError(f"{origin} first user event is not the expected outer prompt")
            skipped_user_id = first_visible.event_id
        response_order: list[str] = []
        groups: dict[str, list[DecodedOpenHandsEvent]] = {}
        for event in decoded:
            if event.response_id:
                if event.response_id not in groups:
                    response_order.append(event.response_id)
                    groups[event.response_id] = []
                groups[event.response_id].append(event)

        projected_groups: dict[str, list[ProjectedStep]] = {}
        incomplete_response: str | None = None
        for response_id in response_order:
            group = groups[response_id]
            actions = [event for event in group if event.kind == "action"]
            observations = [event for event in group if event.kind == "observation"]
            if not actions:
                if final:
                    raise TraceImportError(f"response {response_id} has observations but no actions")
                incomplete_response = response_id
                break
            exemplar = actions[0]
            if any(event.response_fingerprint != exemplar.response_fingerprint for event in group):
                raise TraceImportError(f"response {response_id} repeated with divergent provider payloads")
            call_ids = [call.id for call in exemplar.calls]
            action_by_call = {event.call_id: event for event in actions}
            if len(action_by_call) != len(actions):
                raise TraceImportError(f"response {response_id} repeats an action call id")
            is_finish = len(exemplar.calls) == 1 and exemplar.calls[0].name == "finish"
            finish_actions = [event for event in actions if event.action_name == "finish"]
            finish_calls = [call for call in exemplar.calls if call.name == "finish"]
            if is_finish:
                if len(finish_actions) != 1 or len(actions) != 1:
                    raise TraceImportError(f"finish response {response_id} is not one exact OpenHands finish action")
            elif finish_actions or finish_calls:
                raise TraceImportError(f"response {response_id} mixes finish with non-finish actions")
            complete_actions = set(action_by_call) == set(call_ids)
            obs_by_call = {event.call_id: event for event in observations}
            if len(obs_by_call) != len(observations):
                raise TraceImportError(f"response {response_id} repeats an observation call id")
            if not set(obs_by_call).issubset(set(call_ids)):
                raise TraceImportError(f"response {response_id} contains an unknown observation call id")
            if not complete_actions:
                if final:
                    raise TraceImportError(f"response {response_id} has incomplete action events")
                incomplete_response = response_id
                break
            if is_finish and observations:
                raise TraceImportError(f"finish response {response_id} unexpectedly has an observation")

            reasoning_parts = [event.reasoning for event in actions if event.reasoning]
            reasoning = "\n\n".join(dict.fromkeys(reasoning_parts)) or None
            agent_calls = (
                []
                if is_finish
                else [
                    MCPToolCall(id=call.id, name=call.name, provider_name=call.name, arguments=call.arguments)
                    for call in exemplar.calls
                ]
            )
            response_steps: list[ProjectedStep] = []
            response_steps.append(
                ProjectedStep(
                    f"response:{response_id}",
                    AgentStep(
                        content=exemplar.text,
                        reasoning=reasoning,
                        tool_calls=agent_calls,
                        done=is_finish,
                        finish_reason=exemplar.finish_reason,
                        model=exemplar.model,
                        usage=exemplar.usage,
                        started_at=exemplar.timestamp,
                        ended_at=actions[-1].timestamp,
                    ),
                )
            )
            if not is_finish:
                calls = {call.id: call for call in exemplar.calls}
                for call_id in call_ids:
                    if call_id not in obs_by_call:
                        if final:
                            raise TraceImportError(f"response {response_id} is missing tool result {call_id}")
                        incomplete_response = response_id
                        break
                    event = obs_by_call[call_id]
                    call = calls[call_id]
                    action_event = action_by_call[call_id]
                    if event.cause != action_event.event_id:
                        raise TraceImportError(f"tool result {call_id} does not point to its action")
                    if event.action_name != action_event.action_name:
                        raise TraceImportError(f"tool result {call_id} action type disagrees with its request")
                    response_steps.append(
                        ProjectedStep(
                            f"tool:{call_id}",
                            ToolStep(
                                call=MCPToolCall(
                                    id=call.id, name=call.name, provider_name=call.name, arguments=call.arguments
                                ),
                                result=MCPToolResult(
                                    call_id=call.id,
                                    content=[TextContent(type="text", text=event.text or "")],
                                    isError=event.success is False,
                                ),
                                started_at=event.timestamp,
                                ended_at=event.timestamp,
                            ),
                        )
                    )
            projected_groups[response_id] = response_steps
            if incomplete_response is not None:
                break

        seen_responses: set[str] = set()
        for event in decoded:
            if event.kind == "user":
                if event.event_id == skipped_user_id:
                    continue
                output.append(
                    ProjectedStep(
                        f"user:{event.event_id}",
                        Step(
                            source="user",
                            messages=[
                                PromptMessage(
                                    role="user",
                                    content=TextContent(type="text", text=event.text or ""),
                                )
                            ],
                            started_at=event.timestamp,
                            ended_at=event.timestamp,
                        ),
                    )
                )
            elif event.kind == "agent_text":
                output.append(
                    ProjectedStep(
                        f"agent-message:{event.event_id}",
                        AgentStep(
                            content=event.text,
                            done=False,
                            started_at=event.timestamp,
                            ended_at=event.timestamp,
                        ),
                    )
                )
            elif event.response_id and event.response_id not in seen_responses:
                seen_responses.add(event.response_id)
                if event.response_id == incomplete_response:
                    output.extend(projected_groups.get(event.response_id, ()))
                    break
                output.extend(projected_groups[event.response_id])
        return _normalize_projection_timestamps(output)


def runtime_secret_values(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Select exact secret values without ever retaining or exporting their names."""

    source = os.environ if environ is None else environ
    return tuple(value for name, value in source.items() if value and _RUNTIME_SECRET_NAME.search(name))


def _read_regular_json(path: Path, *, label: str) -> object:
    try:
        info = path.lstat()
    except OSError as exc:
        raise TraceImportError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TraceImportError(f"{label} must be a regular non-symlink file")
    if info.st_size > _MAX_TRACE_BYTES:
        raise TraceImportError(f"{label} exceeds the safe import size")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TraceImportError(f"{label} is not valid UTF-8 JSON") from exc


def _submit_secrets(path: Path | None) -> tuple[str, ...]:
    if path is None or not path.exists():
        return ()
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise TraceImportError("workspace submit.sh is not a safe regular file")
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise TraceImportError("workspace submit.sh could not be read safely") from exc
    values: list[str] = []
    for field in ("agent_id", "checksum"):
        match = re.search(rf'"{field}"\s*:\s*"([^"]+)"', text)
        if match:
            values.append(match.group(1))
    return tuple(values)


def _visible_user_text(step: Step) -> str:
    parts: list[str] = []
    for message in step.messages:
        content = message.content
        if isinstance(content, TextContent) and content.text:
            parts.append(content.text)
    return "\n\n".join(parts)


def _normalize_mcp_result(result: object) -> tuple[str | None, object | None]:
    if not isinstance(result, Mapping):
        return (result, None) if isinstance(result, str) else (None, result)
    result_dict = dict(result)
    if "structuredContent" in result_dict:
        return None, result_dict
    content = result_dict.get("content")
    if isinstance(content, list):
        text_parts = [
            item["text"]
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "text" and isinstance(item.get("text"), str)
        ]
        joined = "\n".join(text_parts) if text_parts else None
        return joined, result_dict if joined is None else None
    if isinstance(content, str):
        return content, None
    return None, result_dict


def _projected_event_view(item: ProjectedStep) -> dict[str, Any]:
    step = item.step
    if step.source == "user":
        return {"kind": "user_message", "text": _visible_user_text(step)}
    if isinstance(step, AgentStep):
        return {
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
    if isinstance(step, ToolStep) and step.call is not None and step.result is not None:
        result = step.result.model_dump(mode="json", exclude_none=True)
        result_text, result_data = _normalize_mcp_result(result)
        is_error = bool(result.get("isError"))
        return {
            "kind": "tool_call",
            "tool_name": step.call.name,
            "arguments": step.call.arguments or {},
            "result_text": None if is_error else result_text,
            "result_data": result_data,
            "error": (step.error or result_text or "tool error (no message)") if is_error or step.error else None,
        }
    raise TraceImportError(f"projected step {item.key} has no control-plane-visible representation")


def _remote_event_view(event: Mapping[str, Any]) -> dict[str, Any]:
    kind = event.get("kind")
    if kind == "user_message":
        return {"kind": kind, "text": event.get("text")}
    if kind == "agent_message":
        raw_calls = event.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise TraceImportError("remote HUD agent tool calls are malformed")
        calls: list[dict[str, Any]] = []
        for raw_call in raw_calls:
            if not isinstance(raw_call, Mapping):
                raise TraceImportError("remote HUD agent tool call is malformed")
            calls.append(
                {
                    "tool_call_id": raw_call.get("tool_call_id"),
                    "name": raw_call.get("name"),
                    "arguments": raw_call.get("arguments") or {},
                }
            )
        return {
            "kind": kind,
            "text": event.get("text"),
            "reasoning": event.get("reasoning"),
            "tool_calls": calls,
        }
    if kind == "tool_call":
        return {
            "kind": kind,
            "tool_name": event.get("tool_name"),
            "arguments": event.get("arguments") or {},
            "result_text": event.get("result_text"),
            "result_data": event.get("result_data"),
            "error": event.get("error"),
        }
    raise TraceImportError("remote HUD event is not part of the imported conversation")


def _canonical_sha256(value: object) -> str:
    try:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    except (TypeError, ValueError) as exc:
        raise TraceImportError("projected HUD conversation is not canonical JSON") from exc
    return hashlib.sha256(canonical).hexdigest()


def build_trace_import_metadata(
    steps: Sequence[ProjectedStep],
    *,
    status: Literal["completed", "partial_error"],
) -> dict[str, Any]:
    """Bind the local projection to the exact control-plane-visible conversation."""

    agent_count = sum(isinstance(item.step, AgentStep) for item in steps)
    tool_count = sum(isinstance(item.step, ToolStep) for item in steps)
    user_count = sum(item.step.source == "user" for item in steps)
    canonical_steps = [{"key": item.key, "step": item.step.model_dump(mode="json")} for item in steps]
    visible_events = [_projected_event_view(item) for item in steps]
    return {
        "schema_version": "1",
        "status": status,
        "projected_step_count": len(steps),
        "agent_step_count": agent_count,
        "tool_step_count": tool_count,
        "user_step_count": user_count,
        "has_final_agent_done": any(isinstance(item.step, AgentStep) and item.step.done for item in steps),
        "source_has_tool_actions": tool_count > 0,
        "projected_steps_sha256": _canonical_sha256(canonical_steps),
        "projected_events_sha256": _canonical_sha256(visible_events),
    }


def import_openhands_trace(
    receipt: NativeReceipt,
    *,
    workspace_submit: Path | None = None,
    redactions: Iterable[str] = (),
    include_initial_user_prompt: bool = False,
) -> TraceImportResult:
    """Load a completed saved trajectory and return only redacted HUD steps."""

    if not receipt.log_dir:
        raise TraceImportError("native receipt has no trajectory directory")
    log_dir = Path(receipt.log_dir)
    args = _mapping(_read_regular_json(log_dir / "args.json", label="OpenHands args.json"), label="OpenHands args")
    task = _mapping(args.get("task"), label="OpenHands task args")
    expected = {
        "task_id": receipt.task_id,
        "server": receipt.server,
        "agent_id": receipt.agent_id,
    }
    for field, expected_value in expected.items():
        if task.get(field) != expected_value:
            raise TraceImportError(f"OpenHands args {field} disagrees with native receipt")
    task_secrets = [task.get("agent_id"), task.get("checksum"), task.get("server")]
    llm = args.get("agent_args", {})
    if isinstance(llm, Mapping):
        llm = llm.get("llm", {})
    if isinstance(llm, Mapping):
        task_secrets.extend(value for key, value in llm.items() if _RUNTIME_SECRET_NAME.search(str(key)))
    all_redactions = [
        value for value in (*task_secrets, *_submit_secrets(workspace_submit), *redactions) if isinstance(value, str)
    ]
    raw_events = _read_regular_json(log_dir / "trajectory", label="OpenHands trajectory")
    if not isinstance(raw_events, list) or not raw_events:
        raise TraceImportError("OpenHands trajectory must be a nonempty event list")
    projector = OpenHandsEventProjector(redactions=all_redactions)
    first_visible = projector.decode(raw_events[0], origin="trajectory")
    if first_visible is None or first_visible.kind != "user" or first_visible.text != OG_PROMPT:
        raise TraceImportError("OpenHands first user event is not the pinned OG prompt")
    steps = projector.project(
        raw_events,
        final=receipt.status == "completed",
        skip_initial_user_prompt=None if include_initial_user_prompt else OG_PROMPT,
    )
    agent_count = sum(isinstance(item.step, AgentStep) for item in steps)
    if agent_count < 1:
        raise TraceImportError("OpenHands trajectory has no model-visible assistant turn")
    # A canonical max-iteration or rejected controller terminal is a normal,
    # gradeable CyberGym endpoint even though CodeAct did not emit its
    # provider ``finish`` tool.  Native controller-state validation decides
    # whether that endpoint is gradeable; the trace importer must preserve the
    # transcript instead of silently converting every bounded failure into
    # infrastructure error.  ``has_final_agent_done`` below still distinguishes
    # an explicit finish from a bounded controller terminal in the receipt.
    metadata = build_trace_import_metadata(
        steps,
        status="completed" if receipt.status == "completed" else "partial_error",
    )
    return TraceImportResult(steps=steps, metadata=metadata)


def map_openhands_receipt(path: Path) -> tuple[ProjectedStep, ...]:
    """Backfill adapter from a mode-safe native receipt JSON sidecar."""

    raw = _read_regular_json(path, label="native receipt sidecar")
    try:
        receipt = NativeReceipt.model_validate(raw)
    except Exception as exc:
        raise TraceImportError("native receipt sidecar does not match the receipt contract") from exc
    return import_openhands_trace(
        receipt,
        redactions=runtime_secret_values(),
        include_initial_user_prompt=True,
    ).steps


def validate_remote_trace_projection(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate that a terminal remote HUD trace contains the imported transcript."""

    receipts: list[Mapping[str, Any]] = []
    for event in events:
        candidates: list[object] = [event, event.get("extra"), event.get("attributes")]
        attributes = event.get("attributes")
        if isinstance(attributes, Mapping):
            candidates.extend((attributes.get("extra"), attributes.get("hud.payload")))
            payload = attributes.get("hud.payload")
            if isinstance(payload, Mapping):
                candidates.append(payload.get("extra"))
        for candidate in candidates:
            if isinstance(candidate, Mapping) and isinstance(candidate.get("openhands_trace_import"), Mapping):
                receipts.append(candidate["openhands_trace_import"])
                break
    if len(receipts) != 1:
        raise TraceImportError("remote HUD trace does not contain exactly one trajectory import receipt")
    receipt = receipts[0]
    local_digest = receipt.get("projected_steps_sha256")
    visible_digest = receipt.get("projected_events_sha256")
    if (
        receipt.get("status") not in {"completed", "partial_error"}
        or not isinstance(local_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", local_digest)
        or not isinstance(visible_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", visible_digest)
    ):
        raise TraceImportError("remote HUD trajectory import receipt is invalid")
    agent_count = receipt.get("agent_step_count")
    tool_count = receipt.get("tool_step_count")
    user_count = receipt.get("user_step_count", 0)
    step_count = receipt.get("projected_step_count")
    if not all(isinstance(value, int) and value >= 0 for value in (agent_count, tool_count, user_count, step_count)):
        raise TraceImportError("remote HUD trajectory import counts are invalid")
    remote_agents = [event for event in events if event.get("kind") == "agent_message"]
    remote_tools = [event for event in events if event.get("kind") == "tool_call"]
    remote_users = [event for event in events if event.get("kind") == "user_message"] if user_count else []
    if not remote_agents or any(
        not (event.get("text") or event.get("reasoning") or event.get("tool_calls")) for event in remote_agents
    ):
        raise TraceImportError("remote HUD trace has no complete model-visible assistant transcript")
    if (
        step_count != agent_count + tool_count + user_count
        or agent_count != len(remote_agents)
        or tool_count != len(remote_tools)
        or (user_count and user_count != len(remote_users))
    ):
        raise TraceImportError("remote HUD trajectory import counts disagree with projected events")
    if receipt.get("source_has_tool_actions") and not remote_tools:
        raise TraceImportError("remote HUD trace lost all source tool calls")
    imported_kinds = {"user_message", "agent_message", "tool_call"} if user_count else {"agent_message", "tool_call"}
    imported_events = [event for event in events if event.get("kind") in imported_kinds]
    if _canonical_sha256([_remote_event_view(event) for event in imported_events]) != visible_digest:
        raise TraceImportError("remote HUD conversation content disagrees with import receipt")
    return dict(receipt)


__all__ = [
    "DecodedOpenHandsEvent",
    "OpenHandsEventProjector",
    "ProjectedStep",
    "TraceImportError",
    "TraceImportResult",
    "build_trace_import_metadata",
    "import_openhands_trace",
    "map_openhands_receipt",
    "runtime_secret_values",
    "validate_remote_trace_projection",
]

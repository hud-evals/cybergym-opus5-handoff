"""Narrow GPT-5.6 Responses compatibility for pinned OpenHands 0.33.

OpenHands 0.33 emits LiteLLM Chat Completions calls.  GPT-5.6 Sol requires
the Responses API when xhigh reasoning and function tools are combined.  This
module is injected only into the OpenHands child process and only for the
explicitly receipted GPT-5.6 Sol/xhigh profile.  It preserves OpenHands'
messages, function schemas, function-call IDs, and LiteLLM ``ModelResponse``
contract while changing only the provider transport.
"""

from __future__ import annotations

import functools
import ipaddress
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

TARGET_MODELS = frozenset({"gpt-5.6-sol", "openai/gpt-5.6-sol"})
API_MODEL = "gpt-5.6-sol"
EFFORT_ENV = "CYBERGYM_REASONING_EFFORT"
RUNTIME_NETWORK_ENV = "CYBERGYM_RUNTIME_NETWORK"
DAYTONA_ACTION_URL_ENV = "CYBERGYM_DAYTONA_ACTION_URL"
SUPPORTED_EFFORT = "xhigh"
SUPPORTED_RUNTIME_NETWORK = "cybergym-no-internet"
SUPPORTED_RUNTIME_SUBNET = ipaddress.ip_network("172.30.0.0/24")
SUPPORTED_RUNTIME_GATEWAY = ipaddress.ip_address("172.30.0.1")
SERVED_MODEL_PATTERN = re.compile(r"^gpt-5\.6-sol(?:-\d{4}-\d{2}-\d{2})?$")
DAYTONA_ACTION_URL_PATTERN = re.compile(r"^http://127\.0\.0\.1:([1-9][0-9]{0,4})$")
STANDARD_INPUT_USD_PER_TOKEN = 5.0 / 1_000_000
STANDARD_CACHED_INPUT_USD_PER_TOKEN = 0.5 / 1_000_000
STANDARD_OUTPUT_USD_PER_TOKEN = 30.0 / 1_000_000
LONG_CONTEXT_INPUT_THRESHOLD = 272_000
_MAX_OUTPUT_ERROR_TYPE: type[Exception] | None = None


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _max_output_tokens_exhausted_error(message: str) -> Exception:
    """Return a retryable error whose exact type survives controller wrapping."""

    global _MAX_OUTPUT_ERROR_TYPE
    if _MAX_OUTPUT_ERROR_TYPE is None:
        from openhands.core.exceptions import LLMNoResponseError

        class CyberGymMaxOutputTokensExhaustedError(LLMNoResponseError):
            """All pinned retries exhausted on Responses max_output_tokens."""

        _MAX_OUTPUT_ERROR_TYPE = CyberGymMaxOutputTokensExhaustedError
    return _MAX_OUTPUT_ERROR_TYPE(message)


def _text_content(content: Any) -> str:
    """Flatten OpenHands' text-only content without changing text order."""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, Sequence) or isinstance(content, bytes | bytearray):
        raise RuntimeError(f"unsupported OpenHands message content: {type(content).__name__}")

    texts: list[str] = []
    for part in content:
        part_type = _field(part, "type")
        if part_type == "text":
            text = _field(part, "text")
            if not isinstance(text, str):
                raise RuntimeError("OpenHands text content is not a string")
            texts.append(text)
        elif part_type == "thinking":
            # OpenAI reasoning is retained through previous_response_id, never
            # reconstructed from another provider's visible thinking blocks.
            continue
        else:
            raise RuntimeError(f"CyberGym GPT-5.6 Responses bridge is text-only; got content type {part_type!r}")
    # Chat text-part semantics concatenate parts; inserting a separator here
    # would change the exact OpenHands prompt bytes.
    return "".join(texts)


def _message_content(content: Any, *, role: str) -> str | list[dict[str, str]]:
    """Preserve OpenHands' individual text parts for Responses messages."""

    if role not in {"system", "user", "assistant"}:
        raise RuntimeError(f"unsupported OpenHands message role: {role!r}")
    if content is None or isinstance(content, str):
        return content or ""
    if not isinstance(content, Sequence) or isinstance(content, bytes | bytearray):
        raise RuntimeError(f"unsupported OpenHands message content: {type(content).__name__}")
    converted: list[dict[str, str]] = []
    for part in content:
        part_type = _field(part, "type")
        if part_type == "text":
            text = _field(part, "text")
            if not isinstance(text, str):
                raise RuntimeError("OpenHands text content is not a string")
            part_kind = "output_text" if role == "assistant" else "input_text"
            converted.append({"type": part_kind, "text": text})
        elif part_type == "thinking":
            continue
        else:
            raise RuntimeError(f"CyberGym GPT-5.6 Responses bridge is text-only; got content type {part_type!r}")
    return converted


def _chat_tools_to_responses(tools: Any) -> list[dict[str, Any]]:
    if tools is None:
        return []
    if not isinstance(tools, Sequence):
        raise RuntimeError("OpenHands tools must be a sequence")

    converted: list[dict[str, Any]] = []
    for tool in tools:
        if _field(tool, "type") != "function":
            raise RuntimeError("CyberGym GPT-5.6 bridge accepts only function tools")
        function = _field(tool, "function")
        if not isinstance(function, Mapping):
            raise RuntimeError("OpenHands function tool is malformed")
        name = function.get("name")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not isinstance(parameters, Mapping):
            raise RuntimeError("OpenHands function tool requires name and parameters")
        response_tool: dict[str, Any] = {
            "type": "function",
            "name": name,
            "parameters": dict(parameters),
        }
        description = function.get("description")
        if isinstance(description, str):
            response_tool["description"] = description
        if "strict" in function:
            response_tool["strict"] = bool(function["strict"])
        converted.append(response_tool)
    return converted


def _tool_choice_to_responses(choice: Any) -> Any:
    if not isinstance(choice, Mapping):
        return choice
    if choice.get("type") != "function":
        return dict(choice)
    function = choice.get("function")
    if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
        raise RuntimeError("OpenHands function tool_choice is malformed")
    return {"type": "function", "name": function["name"]}


def _chat_messages_to_responses(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, Sequence) or isinstance(messages, str | bytes | bytearray):
        raise RuntimeError("OpenHands messages must be a sequence")

    converted: list[dict[str, Any]] = []
    for message in messages:
        role = _field(message, "role")
        if role == "tool":
            call_id = _field(message, "tool_call_id")
            if not isinstance(call_id, str):
                raise RuntimeError("OpenHands tool result is missing tool_call_id")
            converted.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _text_content(_field(message, "content")),
                }
            )
            continue
        if role not in {"system", "user", "assistant"}:
            raise RuntimeError(f"unsupported OpenHands message role: {role!r}")
        tool_calls = _field(message, "tool_calls")
        content = _message_content(_field(message, "content"), role=role)
        # Responses represents assistant function calls as top-level output
        # items.  Do not add an empty assistant message beside a tool-only
        # turn: OpenHands' Chat representation uses ``content=None`` there.
        if content or not tool_calls:
            converted.append({"role": role, "content": content})
        if tool_calls:
            for tool_call in tool_calls:
                function = _field(tool_call, "function")
                call_id = _field(tool_call, "id")
                name = _field(function, "name")
                arguments = _field(function, "arguments")
                if not all(isinstance(value, str) for value in (call_id, name, arguments)):
                    raise RuntimeError("OpenHands assistant function call is malformed")
                converted.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments,
                    }
                )
    return converted


def _continuation_outputs(items: Sequence[dict[str, Any]], pending_call_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    """Validate the exact new items that continue one stored Responses turn."""

    wanted = set(pending_call_ids)
    found: dict[str, dict[str, Any]] = {}
    for item in items:
        if item.get("type") != "function_call_output":
            raise RuntimeError("OpenHands added non-tool history before completing pending Responses calls")
        call_id = item.get("call_id")
        if call_id not in wanted:
            raise RuntimeError(f"unexpected OpenHands result for function call {call_id!r}")
        if call_id in found:
            raise RuntimeError(f"duplicate OpenHands result for function call {call_id}")
        found[call_id] = item
    missing = [call_id for call_id in pending_call_ids if call_id not in found]
    if missing:
        raise RuntimeError(f"OpenHands omitted function results for Responses calls: {missing}")
    return [found[call_id] for call_id in pending_call_ids]


def _reasoning_items(response: Any) -> list[dict[str, Any]]:
    """Retain the replayable reasoning envelope from a stored Response."""

    retained: list[dict[str, Any]] = []
    for item in _field(response, "output", []) or []:
        if _field(item, "type") != "reasoning":
            continue
        item_id = _field(item, "id")
        summary = _field(item, "summary", [])
        status = _field(item, "status")
        if not isinstance(item_id, str) or not isinstance(summary, Sequence):
            raise RuntimeError("OpenAI Responses reasoning item is malformed")
        replay: dict[str, Any] = {
            "id": item_id,
            "type": "reasoning",
            "summary": [dict(part) if isinstance(part, Mapping) else part.model_dump() for part in summary],
        }
        if status is not None:
            replay["status"] = status
        retained.append(replay)
    return retained


def _response_to_model_response(response: Any, requested_model: str) -> tuple[Any, tuple[str, ...]]:
    """Adapt one Responses result to the LiteLLM object OpenHands 0.33 consumes."""

    from litellm.types.utils import ModelResponse, Usage

    status = _field(response, "status")
    error = _field(response, "error")
    if error is not None:
        raise RuntimeError(f"OpenAI Responses request did not complete: status={status!r}, error={error!r}")
    if status == "incomplete":
        incomplete = _field(response, "incomplete_details")
        reason = _field(incomplete, "reason")
        if reason == "max_output_tokens":
            # OpenHands already applies its pinned, bounded LLM retry policy to
            # LLMNoResponseError.  Do not adapt partial output: it can contain
            # truncated tool arguments and is not a canonical CodeAct turn.
            raise _max_output_tokens_exhausted_error(
                "OpenAI Responses exhausted max_output_tokens before producing a complete CodeAct turn"
            )
        raise RuntimeError(
            "OpenAI Responses request did not complete: "
            f"status={status!r}, incomplete_reason={reason!r}, error={error!r}"
        )
    if status != "completed":
        raise RuntimeError(f"OpenAI Responses request did not complete: status={status!r}, error={error!r}")
    served_model = _field(response, "model")
    if not isinstance(served_model, str) or SERVED_MODEL_PATTERN.fullmatch(served_model) is None:
        raise RuntimeError(f"OpenAI Responses served an unexpected model for the GPT-5.6 Sol profile: {served_model!r}")

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    unknown_types: list[str] = []
    for item in _field(response, "output", []) or []:
        item_type = _field(item, "type")
        if item_type == "reasoning":
            # The stored Response retains this item for the continuation.
            continue
        if item_type == "function_call":
            call_id = _field(item, "call_id")
            name = _field(item, "name")
            arguments = _field(item, "arguments")
            if not all(isinstance(value, str) for value in (call_id, name, arguments)):
                raise RuntimeError("OpenAI Responses function call is malformed")
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
            continue
        if item_type == "message":
            for part in _field(item, "content", []) or []:
                part_type = _field(part, "type")
                if part_type == "output_text":
                    text = _field(part, "text")
                    if isinstance(text, str):
                        text_parts.append(text)
                elif part_type == "refusal":
                    refusal = _field(part, "refusal")
                    if isinstance(refusal, str):
                        text_parts.append(refusal)
                else:
                    unknown_types.append(str(part_type))
            continue
        unknown_types.append(str(item_type))

    if unknown_types:
        raise RuntimeError(f"unsupported OpenAI Responses output item types: {sorted(unknown_types)}")
    if not text_parts and not tool_calls:
        incomplete = _field(response, "incomplete_details")
        raise RuntimeError(
            f"OpenAI Responses returned neither text nor a function call; status={status!r}, "
            f"incomplete_details={incomplete!r}"
        )

    usage = _field(response, "usage")
    lite_usage = None
    if usage is not None:
        input_details = _field(usage, "input_tokens_details")
        output_details = _field(usage, "output_tokens_details")
        lite_usage = Usage(
            prompt_tokens=int(_field(usage, "input_tokens", 0) or 0),
            completion_tokens=int(_field(usage, "output_tokens", 0) or 0),
            total_tokens=int(_field(usage, "total_tokens", 0) or 0),
            prompt_tokens_details={"cached_tokens": int(_field(input_details, "cached_tokens", 0) or 0)},
            completion_tokens_details={"reasoning_tokens": int(_field(output_details, "reasoning_tokens", 0) or 0)},
        )

    finish_reason = "tool_calls" if tool_calls else "stop"
    adapted = ModelResponse(
        id=_field(response, "id"),
        created=int(_field(response, "created_at", 0) or 0),
        model=served_model,
        choices=[
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "".join(text_parts) or None,
                    "tool_calls": tool_calls or None,
                },
                "finish_reason": finish_reason,
            }
        ],
        usage=lite_usage,
    )
    if usage is not None:
        input_tokens = int(_field(usage, "input_tokens", 0) or 0)
        cached_tokens = min(
            input_tokens,
            int(_field(_field(usage, "input_tokens_details"), "cached_tokens", 0) or 0),
        )
        output_tokens = int(_field(usage, "output_tokens", 0) or 0)
        long_context = input_tokens > LONG_CONTEXT_INPUT_THRESHOLD
        input_multiplier = 2.0 if long_context else 1.0
        output_multiplier = 1.5 if long_context else 1.0
        cost = (
            (input_tokens - cached_tokens) * STANDARD_INPUT_USD_PER_TOKEN * input_multiplier
            + cached_tokens * STANDARD_CACHED_INPUT_USD_PER_TOKEN * input_multiplier
            + output_tokens * STANDARD_OUTPUT_USD_PER_TOKEN * output_multiplier
        )
        # OpenHands 0.33 first checks this provider-cost header before its old
        # LiteLLM price map, which predates GPT-5.6.
        adapted._hidden_params.setdefault("additional_headers", {})["llm_provider-x-litellm-response-cost"] = str(cost)
    return adapted, tuple(call["id"] for call in tool_calls)


def _translate_retryable_openai_error(exc: Exception) -> Exception | None:
    """Map SDK transport failures into the retry types pinned OpenHands owns."""

    from litellm.exceptions import InternalServerError as LiteLLMInternalServerError
    from litellm.exceptions import RateLimitError as LiteLLMRateLimitError
    from litellm.exceptions import Timeout as LiteLLMTimeout
    from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

    if isinstance(exc, RateLimitError):
        return LiteLLMRateLimitError(
            message=str(exc),
            llm_provider="openai",
            model=API_MODEL,
            response=getattr(exc, "response", None),
        )
    if isinstance(exc, APITimeoutError | APIConnectionError):
        return LiteLLMTimeout(
            message=str(exc),
            model=API_MODEL,
            llm_provider="openai",
        )
    if isinstance(exc, InternalServerError):
        return LiteLLMInternalServerError(
            message=str(exc),
            llm_provider="openai",
            model=API_MODEL,
            response=getattr(exc, "response", None),
        )
    return None


class _ResponsesBridge:
    """Stateful Chat-to-Responses adapter for exactly one OpenHands LLM."""

    def __init__(self, effort: str, client_factory: Callable[..., Any] | None = None) -> None:
        self._effort = effort
        self._client_factory = client_factory
        self._client: Any = None
        self._client_identity: tuple[Any, ...] | None = None
        self._previous_response_id: str | None = None
        self._pending_call_ids: tuple[str, ...] = ()
        self._last_request_items: list[dict[str, Any]] = []
        self._last_assistant_items: list[dict[str, Any]] = []
        self._replay_history: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        self._lock = threading.Lock()

    def _client_for(self, api_key: Any, base_url: Any, timeout: Any) -> Any:
        identity = (api_key, base_url or None, timeout)
        if self._client is not None:
            if identity != self._client_identity:
                raise RuntimeError("OpenAI client configuration changed during one OpenHands rollout")
            return self._client
        if self._client_factory is None:
            from openai import OpenAI

            factory: Callable[..., Any] = OpenAI
        else:
            factory = self._client_factory
        # Pinned LiteLLM gives the OpenAI SDK two retries beneath OpenHands'
        # outer retry decorator. Preserve both historical layers explicitly.
        client_kwargs: dict[str, Any] = {"api_key": api_key, "max_retries": 2}
        if base_url:
            client_kwargs["base_url"] = base_url
        if timeout is not None:
            client_kwargs["timeout"] = timeout
        self._client = factory(**client_kwargs)
        self._client_identity = identity
        return self._client

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return self._call_locked(args, kwargs)

    def _call_locked(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        kwargs = dict(kwargs)
        positional = list(args)
        if "model" not in kwargs and positional:
            kwargs["model"] = positional.pop(0)
        if "messages" not in kwargs and positional:
            kwargs["messages"] = positional.pop(0)
        if positional:
            raise RuntimeError("unsupported positional LiteLLM arguments")

        model = kwargs.pop("model", None)
        if model not in TARGET_MODELS:
            raise RuntimeError(f"CyberGym {self._effort} compatibility is restricted to gpt-5.6-sol; got {model!r}")
        messages = kwargs.pop("messages", None)
        tools = _chat_tools_to_responses(kwargs.pop("tools", None))

        direct_effort = kwargs.pop("reasoning_effort", None)
        extra_body = kwargs.pop("extra_body", None)
        if extra_body is None:
            extra: dict[str, Any] = {}
        elif isinstance(extra_body, Mapping):
            extra = dict(extra_body)
        else:
            raise RuntimeError("extra_body must be a mapping")
        body_effort = extra.pop("reasoning_effort", None)
        # OpenHands 0.33 defaults this field to high.  The isolated profile
        # intentionally replaces that historical default with receipted xhigh.
        for candidate in (direct_effort, body_effort):
            if candidate not in {None, "high", self._effort}:
                raise RuntimeError(f"conflicting reasoning_effort: {candidate!r}")
        # CodeAct metadata is diagnostic and not part of model behavior.  The
        # historical OpenHands wrapper already removes it for direct providers.
        extra.pop("metadata", None)
        if extra:
            raise RuntimeError(f"unsupported Responses extra_body fields: {sorted(extra)}")

        api_key = kwargs.pop("api_key", None)
        base_url = kwargs.pop("base_url", None)
        timeout = kwargs.pop("timeout", None)
        max_output_tokens = kwargs.pop("max_completion_tokens", None)
        legacy_max_tokens = kwargs.pop("max_tokens", None)
        if max_output_tokens is None:
            max_output_tokens = legacy_max_tokens
        elif legacy_max_tokens not in {None, max_output_tokens}:
            raise RuntimeError("conflicting max token parameters")
        tool_choice = _tool_choice_to_responses(kwargs.pop("tool_choice", None))
        parallel_tool_calls = kwargs.pop("parallel_tool_calls", None)

        # These are Chat/LiteLLM controls intentionally absent from the exact
        # Responses profile.  Non-default seed/provider overrides fail closed.
        kwargs.pop("temperature", None)
        kwargs.pop("top_p", None)
        kwargs.pop("stop", None)
        kwargs.pop("drop_params", None)
        if kwargs.pop("seed", None) is not None:
            raise RuntimeError("seed is unsupported by the CyberGym GPT-5.6 Responses profile")
        if kwargs.pop("api_version", None) not in {None, ""}:
            raise RuntimeError("api_version override is unsupported for direct OpenAI Responses")
        if kwargs.pop("custom_llm_provider", None) not in {None, ""}:
            raise RuntimeError("custom_llm_provider is unsupported for direct OpenAI Responses")
        if kwargs.pop("stream", False):
            raise RuntimeError("streaming is unsupported by pinned OpenHands 0.33")
        if kwargs:
            raise RuntimeError(f"unsupported LiteLLM arguments for Responses: {sorted(kwargs)}")

        current_items = _chat_messages_to_responses(messages)
        request: dict[str, Any] = {
            "model": API_MODEL,
            "reasoning": {"effort": self._effort},
            "store": True,
        }
        if tools:
            request["tools"] = tools
        request["input"] = self._with_replayable_reasoning(current_items)
        if self._previous_response_id is not None:
            expected_prefix = self._last_request_items + self._last_assistant_items
            if current_items[: len(expected_prefix)] == expected_prefix:
                continuation = current_items[len(expected_prefix) :]
                if self._pending_call_ids:
                    # A valid CodeAct tool continuation contains only all
                    # function results from the immediately preceding response.
                    continuation = _continuation_outputs(continuation, self._pending_call_ids)
                elif continuation and all(item.get("role") == "user" for item in continuation):
                    # Headless OpenHands turns a text-only MessageAction into
                    # an exact appended user auto-continue message.
                    pass
                else:
                    continuation = []
                if continuation:
                    request["input"] = continuation
                    request["previous_response_id"] = self._previous_response_id
            # Otherwise OpenHands changed its visible history (for example a
            # condenser summary or a separate prompt).  Start a new Responses
            # chain from exactly that current history so the server cannot
            # retain context OpenHands deliberately forgot.
        if max_output_tokens is not None:
            request["max_output_tokens"] = int(max_output_tokens)
        if tool_choice is not None:
            request["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            request["parallel_tool_calls"] = bool(parallel_tool_calls)

        client = self._client_for(api_key, base_url, timeout)
        try:
            response = client.responses.create(**request)
        except Exception as exc:
            translated = _translate_retryable_openai_error(exc)
            if translated is None:
                raise
            raise translated from exc
        adapted, pending_call_ids = _response_to_model_response(response, API_MODEL)
        response_id = _field(response, "id")
        if not isinstance(response_id, str) or not response_id:
            raise RuntimeError("OpenAI Responses result is missing an id")
        self._previous_response_id = response_id
        self._pending_call_ids = pending_call_ids
        self._last_request_items = current_items
        assistant_message = adapted.choices[0].message
        assistant_content = _field(assistant_message, "content")
        self._last_assistant_items = _chat_messages_to_responses(
            [
                {
                    "role": "assistant",
                    # ConversationMemory reconstructs provider text as one
                    # OpenHands TextContent part on the following LLM call.
                    "content": (
                        [{"type": "text", "text": assistant_content}]
                        if isinstance(assistant_content, str)
                        else assistant_content
                    ),
                    "tool_calls": _field(assistant_message, "tool_calls"),
                }
            ]
        )
        self._replay_history.append((self._last_assistant_items, _reasoning_items(response)))
        return adapted

    def _with_replayable_reasoning(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Reinsert known reasoning before historical assistant outputs.

        A condensation reset cannot use the old previous_response_id because
        that would retain history OpenHands removed.  Its surviving tail can
        still contain assistant messages or function-call pairs, however, and
        reasoning models require every corresponding output reasoning item
        during manual replay. Tail-first matching disambiguates repeated text.
        """

        insertions: dict[int, list[dict[str, Any]]] = {}
        search_before = len(items)
        for assistant_items, reasoning in reversed(self._replay_history):
            if not assistant_items or not reasoning:
                continue
            width = len(assistant_items)
            match_at: int | None = None
            for start in range(search_before - width, -1, -1):
                if items[start : start + width] == assistant_items:
                    match_at = start
                    break
            if match_at is None:
                continue
            insertions[match_at] = reasoning
            search_before = match_at

        converted: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            converted.extend(insertions.get(index, []))
            converted.append(item)
        return converted


def _bridge_partial(original: Callable[..., Any], bridge: _ResponsesBridge) -> Callable[..., Any]:
    """Keep the exact kwargs OpenHands pinned into its LiteLLM partial."""

    if not isinstance(original, functools.partial):
        raise RuntimeError("pinned OpenHands LLM transport is no longer a functools.partial")
    return functools.partial(bridge, *original.args, **(original.keywords or {}))


def _patch_llm_instances(llm: Any, effort: str) -> None:
    """Attach an independent Responses chain to each sync LLM instance."""

    cls = llm.LLM
    original_init = cls.__init__
    if getattr(original_init, "_cybergym_gpt56_xhigh", False):
        return

    @functools.wraps(original_init)
    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        if self.config.model not in TARGET_MODELS:
            return
        bridge = _ResponsesBridge(effort)
        self._cybergym_gpt56_responses_bridge = bridge
        # LLM.wrapper dereferences this attribute on every call, so replacing
        # it after the pinned constructor preserves retries/logging/metrics.
        self._completion_unwrapped = _bridge_partial(self._completion_unwrapped, bridge)

    patched_init._cybergym_gpt56_xhigh = True
    cls.__init__ = patched_init


def _patch_async_llm_instances(async_llm: Any, effort: str) -> None:
    """Give AsyncLLM its own per-instance chain without a global singleton."""

    cls = async_llm.AsyncLLM
    original_call = cls._call_acompletion
    if getattr(original_call, "_cybergym_gpt56_xhigh", False):
        return

    @functools.wraps(original_call)
    async def patched_call(self: Any, *args: Any, **kwargs: Any) -> Any:
        if self.config.model not in TARGET_MODELS:
            return await original_call(self, *args, **kwargs)
        bridge = getattr(self, "_cybergym_gpt56_responses_bridge", None)
        if bridge is None:
            raise RuntimeError("target OpenHands AsyncLLM is missing its per-instance Responses bridge")
        return await __import__("asyncio").to_thread(bridge, *args, **kwargs)

    patched_call._cybergym_gpt56_xhigh = True
    cls._call_acompletion = patched_call


def _patch_docker_runtime(docker_runtime: Any, network: str) -> None:
    """Connect the host controller directly to its internal-only container.

    Docker does not publish runtime ports back to localhost for an internal
    bridge. The Linux host can still reach the container address directly.
    Bind only the exact reviewed network/subnet and reject any extra network
    attachment instead of falling back to the public Docker-default route.
    """

    if network != SUPPORTED_RUNTIME_NETWORK:
        raise RuntimeError(f"unsupported CyberGym runtime network: {network!r}")
    cls = docker_runtime.DockerRuntime
    original = cls._init_container
    if getattr(original, "_cybergym_private_runtime_network", False):
        return

    @functools.wraps(original)
    def patched(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        runtime_kwargs = self.config.sandbox.docker_runtime_kwargs
        if not isinstance(runtime_kwargs, Mapping) or runtime_kwargs.get("network") != network:
            raise RuntimeError("OpenHands runtime did not retain the reviewed private-only network")
        self.container.reload()
        attachments = (self.container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
        if set(attachments) != {network}:
            raise RuntimeError(f"OpenHands runtime has unexpected Docker network attachments: {sorted(attachments)}")
        details = attachments.get(network)
        address_text = details.get("IPAddress") if isinstance(details, Mapping) else None
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError as exc:
            raise RuntimeError("OpenHands runtime has no valid private-network address") from exc
        if address not in SUPPORTED_RUNTIME_SUBNET or address == SUPPORTED_RUNTIME_GATEWAY:
            raise RuntimeError(f"OpenHands runtime address is outside the reviewed private subnet: {address}")
        if not isinstance(self._container_port, int) or not 1 <= self._container_port <= 65535:
            raise RuntimeError("OpenHands runtime action-server port is invalid")
        self.api_url = f"http://{address}:{self._container_port}"
        return result

    patched._cybergym_private_runtime_network = True
    cls._init_container = patched


def install() -> bool:
    """Install the exact Responses adapters; return whether activated."""

    effort = os.environ.get(EFFORT_ENV)
    runtime_network = os.environ.get(RUNTIME_NETWORK_ENV)
    daytona_action_url = os.environ.get(DAYTONA_ACTION_URL_ENV)
    if effort is None and runtime_network is None and daytona_action_url is None:
        return False
    if runtime_network is not None and daytona_action_url is not None:
        raise RuntimeError("CyberGym OpenHands child cannot select Docker and Daytona runtimes together")
    daytona_attached = False
    if daytona_action_url is not None:
        match = DAYTONA_ACTION_URL_PATTERN.fullmatch(daytona_action_url)
        if match is None or int(match.group(1)) > 65535:
            raise RuntimeError(f"{DAYTONA_ACTION_URL_ENV} must be an exact loopback HTTP origin")
        daytona_attached = True
    if runtime_network is not None:
        if runtime_network != SUPPORTED_RUNTIME_NETWORK:
            raise RuntimeError(f"{RUNTIME_NETWORK_ENV} must be {SUPPORTED_RUNTIME_NETWORK!r}; got {runtime_network!r}")
        from openhands.runtime.impl.docker import docker_runtime

        _patch_docker_runtime(docker_runtime, runtime_network)
    elif not daytona_attached:
        raise RuntimeError(f"either {RUNTIME_NETWORK_ENV} or a loopback-only {DAYTONA_ACTION_URL_ENV} is required")
    if effort is None:
        return True
    if effort != SUPPORTED_EFFORT:
        raise RuntimeError(f"{EFFORT_ENV} must be {SUPPORTED_EFFORT!r}; got {effort!r}")

    from openhands.llm import async_llm, llm

    for supported_models in (
        llm.FUNCTION_CALLING_SUPPORTED_MODELS,
        llm.REASONING_EFFORT_SUPPORTED_MODELS,
        llm.MODELS_WITHOUT_STOP_WORDS,
    ):
        for model in TARGET_MODELS:
            if model not in supported_models:
                supported_models.append(model)
    _patch_llm_instances(llm, effort)
    _patch_async_llm_instances(async_llm, effort)
    return True

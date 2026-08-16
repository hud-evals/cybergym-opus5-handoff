from __future__ import annotations

import asyncio
import functools
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import openai
import pytest

ROOT = Path(__file__).resolve().parents[3]
COMPAT_PATH = ROOT / "integrations/hud/openhands_shim/_cybergym_openhands_compat.py"
SITECUSTOMIZE_PATH = ROOT / "integrations/hud/openhands_shim/sitecustomize.py"


class AttrDict(dict):
    def __getattr__(self, name):
        return self[name]


class FakeUsage(AttrDict):
    def __init__(self, **kwargs):
        super().__init__(kwargs)


class FakeModelResponse(AttrDict):
    def __init__(self, **kwargs):
        choices = []
        for raw_choice in kwargs.get("choices", []):
            raw_message = raw_choice["message"]
            tool_calls = [
                AttrDict(
                    id=call["id"],
                    type=call["type"],
                    function=AttrDict(**call["function"]),
                )
                for call in (raw_message.get("tool_calls") or [])
            ]
            message = AttrDict(**raw_message)
            message["tool_calls"] = tool_calls or None
            choices.append(AttrDict(**{**raw_choice, "message": message}))
        super().__init__({**kwargs, "choices": choices})
        self._hidden_params = {}


class FakeLiteLLMRateLimitError(Exception):
    def __init__(self, message, **kwargs):
        super().__init__(message)
        self.kwargs = kwargs


class FakeLiteLLMTimeout(Exception):
    def __init__(self, message, **kwargs):
        super().__init__(message)
        self.kwargs = kwargs


class FakeLiteLLMInternalServerError(Exception):
    def __init__(self, message, **kwargs):
        super().__init__(message)
        self.kwargs = kwargs


class FakeLLMNoResponseError(Exception):
    pass


@pytest.fixture(autouse=True)
def _pinned_litellm_types(monkeypatch: pytest.MonkeyPatch):
    """The integration venv omits LiteLLM; the pinned OH venv supplies it."""

    litellm = ModuleType("litellm")
    types_module = ModuleType("litellm.types")
    utils_module = ModuleType("litellm.types.utils")
    exceptions_module = ModuleType("litellm.exceptions")
    utils_module.ModelResponse = FakeModelResponse
    utils_module.Usage = FakeUsage
    types_module.utils = utils_module
    litellm.types = types_module
    exceptions_module.RateLimitError = FakeLiteLLMRateLimitError
    exceptions_module.Timeout = FakeLiteLLMTimeout
    exceptions_module.InternalServerError = FakeLiteLLMInternalServerError
    openhands_module = ModuleType("openhands")
    core_module = ModuleType("openhands.core")
    core_exceptions_module = ModuleType("openhands.core.exceptions")
    core_exceptions_module.LLMNoResponseError = FakeLLMNoResponseError
    core_module.exceptions = core_exceptions_module
    openhands_module.core = core_module
    monkeypatch.setitem(sys.modules, "litellm", litellm)
    monkeypatch.setitem(sys.modules, "litellm.types", types_module)
    monkeypatch.setitem(sys.modules, "litellm.types.utils", utils_module)
    monkeypatch.setitem(sys.modules, "litellm.exceptions", exceptions_module)
    monkeypatch.setitem(sys.modules, "openhands", openhands_module)
    monkeypatch.setitem(sys.modules, "openhands.core", core_module)
    monkeypatch.setitem(sys.modules, "openhands.core.exceptions", core_exceptions_module)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_test_cybergym_openhands_compat", COMPAT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response(response_id: str, output: list[dict], *, input_tokens: int = 11, output_tokens: int = 7):
    return {
        "id": response_id,
        "created_at": 123.0,
        "model": "gpt-5.6-sol",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": output,
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": input_tokens + output_tokens,
        },
    }


class FakeResponses:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return next(self._responses)


class FakeClient:
    def __init__(self, responses):
        self.responses = responses


def test_two_turn_responses_bridge_preserves_codeact_tools_and_call_ids() -> None:
    compat = _load()
    api = FakeResponses(
        [
            _response(
                "resp_1",
                [
                    {"id": "rs_1", "type": "reasoning", "summary": []},
                    {
                        "id": "fc_1",
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "execute_bash",
                        "arguments": '{"command":"pwd"}',
                    },
                ],
            ),
            _response(
                "resp_2",
                [
                    {
                        "id": "msg_2",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done", "annotations": []}],
                    }
                ],
            ),
        ]
    )
    factory_calls = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return FakeClient(api)

    bridge = compat._ResponsesBridge("xhigh", client_factory=factory)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "execute_bash",
                "description": "Run a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        }
    ]
    first_messages = [
        {"role": "system", "content": [{"type": "text", "text": "CodeAct system"}]},
        {"role": "user", "content": [{"type": "text", "text": "Inspect the workspace"}]},
    ]
    first = bridge(
        model="openai/gpt-5.6-sol",
        api_key="local-fake-key",
        base_url="http://127.0.0.1:1/v1",
        timeout=30,
        messages=first_messages,
        tools=tools,
        reasoning_effort="high",
        temperature=0,
        top_p=1,
        stop=["<stop>"],
        max_completion_tokens=2048,
        drop_params=True,
        extra_body={"metadata": {"trace": "diagnostic"}},
    )

    assert first.choices[0].message.tool_calls[0].id == "call_1"
    assert first.choices[0].message.tool_calls[0].function.name == "execute_bash"
    assert first.usage.prompt_tokens == 11
    assert factory_calls == [
        {
            "api_key": "local-fake-key",
            "max_retries": 2,
            "base_url": "http://127.0.0.1:1/v1",
            "timeout": 30,
        }
    ]
    assert float(first._hidden_params["additional_headers"]["llm_provider-x-litellm-response-cost"]) == pytest.approx(
        0.0002515
    )
    first_request = api.requests[0]
    assert first_request == {
        "model": "gpt-5.6-sol",
        "reasoning": {"effort": "xhigh"},
        "store": True,
        "tools": [
            {
                "type": "function",
                "name": "execute_bash",
                "description": "Run a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            }
        ],
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": "CodeAct system"}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Inspect the workspace"}],
            },
        ],
        "max_output_tokens": 2048,
    }
    assert not {"temperature", "top_p", "stop", "reasoning_effort"} & first_request.keys()

    second_messages = [
        *first_messages,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "execute_bash", "arguments": '{"command":"pwd"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "execute_bash", "content": "/workspace\n"},
    ]
    second = bridge(
        model="openai/gpt-5.6-sol",
        api_key="local-fake-key",
        base_url="http://127.0.0.1:1/v1",
        timeout=30,
        messages=second_messages,
        tools=tools,
        max_completion_tokens=2048,
    )
    assert second.choices[0].message.content == "done"
    assert api.requests[1]["previous_response_id"] == "resp_1"
    assert api.requests[1]["input"] == [{"type": "function_call_output", "call_id": "call_1", "output": "/workspace\n"}]
    assert first_messages[0] not in api.requests[1]["input"]


def test_bridge_fails_closed_on_model_effort_and_call_id_drift() -> None:
    compat = _load()
    bridge = compat._ResponsesBridge("xhigh", client_factory=lambda **_kwargs: pytest.fail("no request expected"))
    with pytest.raises(RuntimeError, match="restricted"):
        bridge(model="openai/gpt-5.6-terra", messages=[])
    with pytest.raises(RuntimeError, match="conflicting"):
        bridge(model="openai/gpt-5.6-sol", messages=[], reasoning_effort="low")

    api = FakeResponses(
        [
            _response(
                "resp_1",
                [
                    {
                        "type": "function_call",
                        "call_id": "call_required",
                        "name": "execute_bash",
                        "arguments": "{}",
                    }
                ],
            )
        ]
    )
    active = compat._ResponsesBridge("xhigh", client_factory=lambda **_kwargs: FakeClient(api))
    active(model="gpt-5.6-sol", messages=[], tools=[])
    with pytest.raises(RuntimeError, match="unexpected OpenHands result"):
        active(
            model="gpt-5.6-sol",
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_required",
                            "type": "function",
                            "function": {"name": "execute_bash", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "wrong", "name": "execute_bash", "content": "x"},
            ],
            tools=[],
        )

    unexpected = FakeResponses(
        [_response("resp_wrong", [{"type": "message", "content": [{"type": "output_text", "text": "x"}]}])]
    )
    unexpected._responses = iter(
        [
            {
                **_response(
                    "resp_wrong",
                    [{"type": "message", "content": [{"type": "output_text", "text": "x"}]}],
                ),
                "model": "gpt-5.6-terra",
            }
        ]
    )
    wrong_model = compat._ResponsesBridge("xhigh", client_factory=lambda **_kwargs: FakeClient(unexpected))
    with pytest.raises(RuntimeError, match="unexpected model"):
        wrong_model(model="gpt-5.6-sol", messages=[])

    empty = FakeResponses([_response("resp_empty", [{"id": "rs", "type": "reasoning", "summary": []}])])
    empty_output = compat._ResponsesBridge("xhigh", client_factory=lambda **_kwargs: FakeClient(empty))
    with pytest.raises(RuntimeError, match="neither text nor a function call"):
        empty_output(model="gpt-5.6-sol", messages=[])

    incomplete_api = FakeResponses(
        [
            {
                **_response(
                    "resp_incomplete",
                    [{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}],
                ),
                "status": "incomplete",
            }
        ]
    )
    incomplete = compat._ResponsesBridge("xhigh", client_factory=lambda **_kwargs: FakeClient(incomplete_api))
    with pytest.raises(RuntimeError, match="did not complete"):
        incomplete(model="gpt-5.6-sol", messages=[])

    max_output_api = FakeResponses(
        [
            {
                **_response("resp_max_output", [{"id": "rs", "type": "reasoning", "summary": []}]),
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            }
        ]
    )
    max_output = compat._ResponsesBridge("xhigh", client_factory=lambda **_kwargs: FakeClient(max_output_api))
    with pytest.raises(FakeLLMNoResponseError, match="max_output_tokens"):
        max_output(model="gpt-5.6-sol", messages=[])

    filtered_api = FakeResponses(
        [
            {
                **_response("resp_filtered", []),
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
            }
        ]
    )
    filtered = compat._ResponsesBridge("xhigh", client_factory=lambda **_kwargs: FakeClient(filtered_api))
    with pytest.raises(RuntimeError, match="content_filter"):
        filtered(model="gpt-5.6-sol", messages=[])


def test_multipart_message_text_preserves_each_responses_input_part() -> None:
    compat = _load()
    converted = compat._chat_messages_to_responses(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},
                ],
            }
        ]
    )
    assert converted == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "first"},
                {"type": "input_text", "text": "second"},
            ],
        }
    ]
    assert (
        compat._text_content([{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]) == "firstsecond"
    )


def test_sdk_retry_count_and_connection_error_preserve_openhands_outer_retry() -> None:
    compat = _load()
    request = httpx.Request("POST", "http://127.0.0.1:1/v1/responses")

    class RaisingResponses:
        def create(self, **_kwargs):
            raise openai.APIConnectionError(request=request)

    factory_calls = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return FakeClient(RaisingResponses())

    bridge = compat._ResponsesBridge("xhigh", client_factory=factory)
    with pytest.raises(FakeLiteLLMTimeout) as caught:
        bridge(model="gpt-5.6-sol", api_key="fake", messages=[])
    assert caught.value.kwargs == {"model": "gpt-5.6-sol", "llm_provider": "openai"}
    assert factory_calls == [{"api_key": "fake", "max_retries": 2}]


def test_bridge_preserves_multiple_parallel_function_results_in_call_order() -> None:
    compat = _load()
    api = FakeResponses(
        [
            _response(
                "resp_many",
                [
                    {"type": "function_call", "call_id": "call_a", "name": "execute_bash", "arguments": "{}"},
                    {"type": "function_call", "call_id": "call_b", "name": "think", "arguments": "{}"},
                ],
            ),
            _response(
                "resp_done",
                [{"type": "message", "content": [{"type": "output_text", "text": "complete"}]}],
            ),
        ]
    )
    bridge = compat._ResponsesBridge("xhigh", client_factory=lambda **_kwargs: FakeClient(api))
    first = bridge(model="gpt-5.6-sol", messages=[], tools=[])
    assert [call.id for call in first.choices[0].message.tool_calls] == ["call_a", "call_b"]
    second = bridge(
        model="gpt-5.6-sol",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "execute_bash", "arguments": "{}"},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "think", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_b", "name": "think", "content": "B"},
            {"role": "tool", "tool_call_id": "call_a", "name": "execute_bash", "content": "A"},
        ],
        tools=[],
    )
    assert second.choices[0].message.content == "complete"
    assert api.requests[1]["input"] == [
        {"type": "function_call_output", "call_id": "call_a", "output": "A"},
        {"type": "function_call_output", "call_id": "call_b", "output": "B"},
    ]


def test_text_only_auto_continue_preserves_reasoning_with_exact_user_delta() -> None:
    compat = _load()
    api = FakeResponses(
        [
            _response(
                "resp_1",
                [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "I will continue.", "annotations": []}],
                    }
                ],
            ),
            _response(
                "resp_2",
                [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}],
            ),
        ]
    )
    bridge = compat._ResponsesBridge("xhigh", client_factory=lambda **_kwargs: FakeClient(api))
    original = [{"role": "user", "content": "work"}]
    first = bridge(model="gpt-5.6-sol", messages=original)
    assert first.choices[0].message.content == "I will continue."
    continued = [
        *original,
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "I will continue."}],
        },
        {"role": "user", "content": [{"type": "text", "text": "CONTINUE"}]},
    ]
    second = bridge(model="gpt-5.6-sol", messages=continued)
    assert second.choices[0].message.content == "done"
    assert api.requests[1]["previous_response_id"] == "resp_1"
    assert api.requests[1]["input"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "CONTINUE"}],
        }
    ]


def test_condensed_history_restarts_chain_instead_of_retaining_forgotten_context() -> None:
    compat = _load()
    api = FakeResponses(
        [
            _response(
                "resp_1",
                [
                    {
                        "id": "rs_1",
                        "type": "reasoning",
                        "summary": [],
                        "status": "completed",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "execute_bash",
                        "arguments": "{}",
                    },
                ],
            ),
            _response(
                "resp_2",
                [{"type": "message", "content": [{"type": "output_text", "text": "new chain"}]}],
            ),
        ]
    )
    bridge = compat._ResponsesBridge("xhigh", client_factory=lambda **_kwargs: FakeClient(api))
    bridge(model="gpt-5.6-sol", messages=[{"role": "user", "content": "long private history"}])
    condensed = [
        {"role": "system", "content": "summary"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "execute_bash", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "execute_bash",
            "content": "retained tail output",
        },
        {"role": "user", "content": "continue from summary"},
    ]
    result = bridge(model="gpt-5.6-sol", messages=condensed)
    assert result.choices[0].message.content == "new chain"
    assert "previous_response_id" not in api.requests[1]
    assert api.requests[1]["input"] == [
        {"role": "system", "content": "summary"},
        {"id": "rs_1", "type": "reasoning", "summary": [], "status": "completed"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "execute_bash",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "retained tail output",
        },
        {"role": "user", "content": "continue from summary"},
    ]
    reasoning_index = next(
        index for index, item in enumerate(api.requests[1]["input"]) if item.get("type") == "reasoning"
    )
    call_index = next(
        index for index, item in enumerate(api.requests[1]["input"]) if item.get("type") == "function_call"
    )
    assert reasoning_index < call_index
    assert "long private history" not in repr(api.requests[1])


def test_condensed_text_tail_replays_latest_matching_reasoning_item() -> None:
    compat = _load()
    api = FakeResponses(
        [
            _response(
                "resp_text_1",
                [
                    {"id": "rs_old", "type": "reasoning", "summary": [], "status": "completed"},
                    {"type": "message", "content": [{"type": "output_text", "text": "same"}]},
                ],
            ),
            _response(
                "resp_text_2",
                [
                    {"id": "rs_new", "type": "reasoning", "summary": [], "status": "completed"},
                    {"type": "message", "content": [{"type": "output_text", "text": "same"}]},
                ],
            ),
            _response(
                "resp_text_3",
                [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}],
            ),
        ]
    )
    bridge = compat._ResponsesBridge("xhigh", client_factory=lambda **_kwargs: FakeClient(api))
    original = [{"role": "user", "content": "old context"}]
    bridge(model="gpt-5.6-sol", messages=original)
    bridge(
        model="gpt-5.6-sol",
        messages=[
            *original,
            {"role": "assistant", "content": [{"type": "text", "text": "same"}]},
            {"role": "user", "content": [{"type": "text", "text": "continue"}]},
        ],
    )
    bridge(
        model="gpt-5.6-sol",
        messages=[
            {"role": "system", "content": "condensed"},
            {"role": "assistant", "content": [{"type": "text", "text": "same"}]},
            {"role": "user", "content": "new context"},
        ],
    )
    assert "previous_response_id" not in api.requests[2]
    assert api.requests[2]["input"] == [
        {"role": "system", "content": "condensed"},
        {"id": "rs_new", "type": "reasoning", "summary": [], "status": "completed"},
        {
            "role": "assistant",
            "content": [{"type": "input_text", "text": "same"}],
        },
        {"role": "user", "content": "new context"},
    ]
    assert "rs_old" not in repr(api.requests[2])


def test_interleaved_llm_instances_keep_response_ids_and_call_ids_isolated() -> None:
    compat = _load()
    main_api = FakeResponses(
        [
            _response(
                "resp_main",
                [
                    {
                        "type": "function_call",
                        "call_id": "call_main",
                        "name": "execute_bash",
                        "arguments": "{}",
                    }
                ],
            ),
            _response(
                "resp_main_done",
                [{"type": "message", "content": [{"type": "output_text", "text": "main done"}]}],
            ),
        ]
    )
    condenser_api = FakeResponses(
        [
            _response(
                "resp_condenser",
                [{"type": "message", "content": [{"type": "output_text", "text": "summary"}]}],
            )
        ]
    )
    main = compat._ResponsesBridge("xhigh", client_factory=lambda **_kwargs: FakeClient(main_api))
    condenser = compat._ResponsesBridge("xhigh", client_factory=lambda **_kwargs: FakeClient(condenser_api))
    main_messages = [{"role": "user", "content": "main task"}]
    first = main(model="gpt-5.6-sol", messages=main_messages)
    assert first.choices[0].message.tool_calls[0].id == "call_main"
    summary = condenser(model="gpt-5.6-sol", messages=[{"role": "user", "content": "summarize history"}])
    assert summary.choices[0].message.content == "summary"
    main(
        model="gpt-5.6-sol",
        messages=[
            *main_messages,
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_main",
                        "type": "function",
                        "function": {"name": "execute_bash", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_main", "content": "main output"},
        ],
    )
    assert main_api.requests[1]["previous_response_id"] == "resp_main"
    assert main_api.requests[1]["input"][0]["call_id"] == "call_main"
    assert "previous_response_id" not in condenser_api.requests[0]
    assert "tools" not in condenser_api.requests[0]
    assert "resp_condenser" not in repr(main_api.requests)


def test_site_install_patches_pinned_openhands_aliases_and_model_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compat = _load()

    def completion(**_kwargs):
        return "old-sync"

    async def acompletion(**_kwargs):
        return "old-async"

    class FakeLLM:
        def __init__(self, model="openai/gpt-5.6-sol", *_args, **_kwargs):
            self.config = SimpleNamespace(model=model)
            self._completion_unwrapped = functools.partial(
                completion,
                model=model,
                api_key="fake",
                timeout=30,
            )

    class FakeAsyncLLM(FakeLLM):
        async def _call_acompletion(self, *_args, **_kwargs):
            return await acompletion()

    litellm = ModuleType("litellm")
    litellm.completion = completion
    litellm.acompletion = acompletion
    llm_module = ModuleType("openhands.llm.llm")
    llm_module.litellm_completion = completion
    llm_module.LLM = FakeLLM
    llm_module.FUNCTION_CALLING_SUPPORTED_MODELS = []
    llm_module.REASONING_EFFORT_SUPPORTED_MODELS = []
    llm_module.MODELS_WITHOUT_STOP_WORDS = []
    async_module = ModuleType("openhands.llm.async_llm")
    async_module.litellm_acompletion = acompletion
    async_module.AsyncLLM = FakeAsyncLLM
    package = ModuleType("openhands.llm")
    package.llm = llm_module
    package.async_llm = async_module
    openhands = ModuleType("openhands")
    openhands.llm = package
    for name, value in {
        "litellm": litellm,
        "openhands": openhands,
        "openhands.llm": package,
        "openhands.llm.llm": llm_module,
        "openhands.llm.async_llm": async_module,
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    monkeypatch.setenv("CYBERGYM_REASONING_EFFORT", "xhigh")

    assert compat.install() is True
    # The process-global LiteLLM transports stay untouched.  Each OpenHands
    # LLM gets a separate stateful bridge after its pinned constructor runs.
    assert llm_module.litellm_completion is completion
    assert async_module.litellm_acompletion is acompletion
    first = FakeLLM()
    second = FakeLLM()
    assert first._cybergym_gpt56_responses_bridge is not second._cybergym_gpt56_responses_bridge
    assert first._completion_unwrapped.func is first._cybergym_gpt56_responses_bridge
    assert first._completion_unwrapped.keywords == {
        "model": "openai/gpt-5.6-sol",
        "api_key": "fake",
        "timeout": 30,
    }
    non_target = FakeLLM("openai/gpt-4.1")
    assert not hasattr(non_target, "_cybergym_gpt56_responses_bridge")
    assert non_target._completion_unwrapped.func is completion

    target_async = FakeAsyncLLM()
    original_bridge = target_async._cybergym_gpt56_responses_bridge

    class AsyncRecorder:
        def __init__(self):
            self.calls = []

        def __call__(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return "same-instance-bridge"

    recorder = AsyncRecorder()
    target_async._cybergym_gpt56_responses_bridge = recorder
    assert asyncio.run(target_async._call_acompletion(messages=[])) == "same-instance-bridge"
    assert recorder.calls == [((), {"messages": []})]
    assert original_bridge is not recorder

    non_target_async = FakeAsyncLLM("openai/gpt-4.1")
    assert asyncio.run(non_target_async._call_acompletion(messages=[])) == "old-async"
    assert FakeAsyncLLM._call_acompletion._cybergym_gpt56_xhigh is True
    for values in (
        llm_module.FUNCTION_CALLING_SUPPORTED_MODELS,
        llm_module.REASONING_EFFORT_SUPPORTED_MODELS,
        llm_module.MODELS_WITHOUT_STOP_WORDS,
    ):
        assert {"gpt-5.6-sol", "openai/gpt-5.6-sol"} <= set(values)


def test_site_install_is_inert_without_explicit_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    compat = _load()
    monkeypatch.delenv("CYBERGYM_REASONING_EFFORT", raising=False)
    assert compat.install() is False


def test_sitecustomize_is_inert_in_poetry_launcher_without_openhands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CYBERGYM_REASONING_EFFORT", "xhigh")
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: None)
    monkeypatch.delitem(sys.modules, "_cybergym_openhands_compat", raising=False)
    spec = importlib.util.spec_from_file_location("_test_sitecustomize", SITECUSTOMIZE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._inside_pinned_openhands_runtime() is False

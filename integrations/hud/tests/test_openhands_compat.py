from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMPAT_PATH = ROOT / "integrations/hud/openhands_shim/_cybergym_openhands_compat.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_test_cybergym_openhands_compat", COMPAT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_transport_wrapper_forces_exact_sol_xhigh_shape() -> None:
    compat = _load()
    captured: dict[str, object] = {}

    def completion(**kwargs):
        captured.update(kwargs)
        return "ok"

    wrapped = compat._wrap_sync(completion, "xhigh")
    assert (
        wrapped(
            model="openai/gpt-5.6-sol",
            reasoning_effort="high",
            temperature=0.0,
            top_p=1.0,
            stop=["<stop>"],
            extra_body={"existing": True},
        )
        == "ok"
    )
    assert captured == {
        "model": "openai/gpt-5.6-sol",
        "extra_body": {"existing": True, "reasoning_effort": "xhigh"},
    }


@pytest.mark.asyncio
async def test_async_transport_wrapper_and_conflicts_fail_closed() -> None:
    compat = _load()
    captured: dict[str, object] = {}

    async def completion(**kwargs):
        captured.update(kwargs)
        return "ok"

    wrapped = compat._wrap_async(completion, "xhigh")
    assert await wrapped(model="gpt-5.6-sol") == "ok"
    assert captured["extra_body"] == {"reasoning_effort": "xhigh"}

    with pytest.raises(RuntimeError, match="restricted"):
        compat._prepare((), {"model": "gpt-5.6-terra"}, "xhigh")
    with pytest.raises(RuntimeError, match="conflicting"):
        compat._prepare((), {"model": "gpt-5.6-sol", "reasoning_effort": "low"}, "xhigh")
    with pytest.raises(RuntimeError, match="conflicting"):
        compat._prepare(
            (),
            {"model": "gpt-5.6-sol", "extra_body": {"reasoning_effort": "max"}},
            "xhigh",
        )


def test_site_install_patches_pinned_openhands_aliases_and_model_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compat = _load()
    captured: dict[str, object] = {}

    def completion(**kwargs):
        captured.update(kwargs)
        return "sync"

    async def acompletion(**_kwargs):
        return "async"

    litellm = ModuleType("litellm")
    litellm.completion = completion
    litellm.acompletion = acompletion
    llm_module = ModuleType("openhands.llm.llm")
    llm_module.litellm_completion = completion
    llm_module.FUNCTION_CALLING_SUPPORTED_MODELS = []
    llm_module.REASONING_EFFORT_SUPPORTED_MODELS = []
    llm_module.MODELS_WITHOUT_STOP_WORDS = []
    async_module = ModuleType("openhands.llm.async_llm")
    async_module.litellm_acompletion = acompletion
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
    assert llm_module.litellm_completion is litellm.completion
    assert async_module.litellm_acompletion is litellm.acompletion
    for values in (
        llm_module.FUNCTION_CALLING_SUPPORTED_MODELS,
        llm_module.REASONING_EFFORT_SUPPORTED_MODELS,
        llm_module.MODELS_WITHOUT_STOP_WORDS,
    ):
        assert {"gpt-5.6-sol", "openai/gpt-5.6-sol"} <= set(values)
    assert llm_module.litellm_completion(model="openai/gpt-5.6-sol") == "sync"
    assert captured["extra_body"] == {"reasoning_effort": "xhigh"}
    assert asyncio.run(async_module.litellm_acompletion(model="gpt-5.6-sol")) == "async"


def test_site_install_is_inert_without_explicit_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    compat = _load()
    monkeypatch.delenv("CYBERGYM_REASONING_EFFORT", raising=False)
    assert compat.install() is False

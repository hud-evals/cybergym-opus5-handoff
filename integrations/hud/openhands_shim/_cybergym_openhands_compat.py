"""Narrow GPT-5.6 transport compatibility for pinned OpenHands 0.33.

The historical OpenHands/LiteLLM lock predates GPT-5.6 and silently drops its
reasoning effort.  This module is injected only into the OpenHands child
process and only for the explicitly receipted GPT-5.6 Sol/xhigh profile.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

TARGET_MODELS = frozenset({"gpt-5.6-sol", "openai/gpt-5.6-sol"})
EFFORT_ENV = "CYBERGYM_REASONING_EFFORT"
SUPPORTED_EFFORT = "xhigh"


def _prepare(args: tuple[Any, ...], kwargs: dict[str, Any], effort: str) -> dict[str, Any]:
    model = kwargs.get("model")
    if model is None and args:
        model = args[0]
    if model not in TARGET_MODELS:
        raise RuntimeError(f"CyberGym {effort} compatibility is restricted to gpt-5.6-sol; got {model!r}")

    direct = kwargs.pop("reasoning_effort", None)
    # OpenHands 0.33 defaults this field to "high" before the requested
    # modern model existed. The isolated profile intentionally replaces that
    # historical default with the receipted xhigh value.
    if direct not in {None, "high", effort}:
        raise RuntimeError(f"conflicting reasoning_effort: {direct!r}")

    raw_extra = kwargs.pop("extra_body", None)
    if raw_extra is None:
        extra: dict[str, Any] = {}
    elif isinstance(raw_extra, Mapping):
        extra = dict(raw_extra)
    else:
        raise RuntimeError("extra_body must be a mapping")
    existing = extra.get("reasoning_effort")
    if existing not in {None, effort}:
        raise RuntimeError(f"conflicting extra_body reasoning_effort: {existing!r}")
    extra["reasoning_effort"] = effort
    kwargs["extra_body"] = extra

    # GPT-5 reasoning requests reject sampling controls. The historical
    # scaffold writes its paper-era defaults into config.toml, so omit them at
    # the transport boundary without changing the scaffold or prompt.
    kwargs.pop("temperature", None)
    kwargs.pop("top_p", None)
    kwargs.pop("stop", None)
    return kwargs


def _wrap_sync(original: Callable[..., Any], effort: str) -> Callable[..., Any]:
    @functools.wraps(original)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        return original(*args, **_prepare(args, kwargs, effort))

    return wrapped


def _wrap_async(original: Callable[..., Awaitable[Any]], effort: str) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(original)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        return await original(*args, **_prepare(args, kwargs, effort))

    return wrapped


def install() -> bool:
    """Install the exact compatibility wrappers; return whether activated."""

    effort = os.environ.get(EFFORT_ENV)
    if effort is None:
        return False
    if effort != SUPPORTED_EFFORT:
        raise RuntimeError(f"{EFFORT_ENV} must be {SUPPORTED_EFFORT!r}; got {effort!r}")

    import litellm
    from openhands.llm import async_llm, llm

    if getattr(litellm.completion, "_cybergym_gpt56_xhigh", False):
        return True
    sync = _wrap_sync(litellm.completion, effort)
    async_ = _wrap_async(litellm.acompletion, effort)
    sync._cybergym_gpt56_xhigh = True
    async_._cybergym_gpt56_xhigh = True
    litellm.completion = sync
    litellm.acompletion = async_
    llm.litellm_completion = sync
    async_llm.litellm_acompletion = async_
    for supported_models in (
        llm.FUNCTION_CALLING_SUPPORTED_MODELS,
        llm.REASONING_EFFORT_SUPPORTED_MODELS,
        llm.MODELS_WITHOUT_STOP_WORDS,
    ):
        for model in TARGET_MODELS:
            if model not in supported_models:
                supported_models.append(model)
    return True

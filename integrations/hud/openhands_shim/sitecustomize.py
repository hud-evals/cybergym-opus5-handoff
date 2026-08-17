"""Python startup hook for the integration-owned OpenHands compatibility."""

from __future__ import annotations

import importlib.util
import os


def _inside_pinned_openhands_runtime() -> bool:
    """Stay inert in Poetry's launcher interpreter and unrelated children."""

    if (
        os.environ.get("CYBERGYM_REASONING_EFFORT") is None
        and os.environ.get("CYBERGYM_RUNTIME_NETWORK") is None
        and os.environ.get("CYBERGYM_DAYTONA_ACTION_URL") is None
        and os.environ.get("CYBERGYM_ANTHROPIC_MODEL") is None
    ):
        return False
    return importlib.util.find_spec("litellm") is not None and importlib.util.find_spec("openhands") is not None


if _inside_pinned_openhands_runtime():
    from _cybergym_openhands_compat import install

    install()

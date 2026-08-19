"""Install the pinned CyberGym compatibility before running OpenHands."""

from __future__ import annotations

import runpy

from _cybergym_openhands_compat import install

if not install():
    raise RuntimeError("CyberGym OpenHands compatibility was not activated")

runpy.run_module("openhands.core.main", run_name="__main__", alter_sys=True)

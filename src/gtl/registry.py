"""Collect the agents each lane provides."""
from __future__ import annotations

import importlib
from typing import Any

MODULES = ("gtl.read", "gtl.trust", "gtl.plan", "gtl.resolve", "gtl.comms", "gtl.govern")


def load_agents() -> list[Any]:
    agents: list[Any] = []
    for name in MODULES:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        agents.extend(getattr(mod, "AGENTS", []))
    return agents

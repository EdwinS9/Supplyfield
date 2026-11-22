"""Governance: the gate, plus the executor and propagator written by another builder."""
from __future__ import annotations

from typing import Any

from gtl.govern.gate import GateAgent

AGENTS: list[Any] = [GateAgent()]

try:
    from gtl.govern.executor import ExecutorAgent
    AGENTS.append(ExecutorAgent())
except ImportError:
    pass

try:
    from gtl.govern.propagator import PropagatorAgent
    AGENTS.append(PropagatorAgent())
except ImportError:
    pass

__all__ = ["AGENTS", "GateAgent"]

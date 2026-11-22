"""Communication ports and the agents that use them."""

from __future__ import annotations

from typing import Any

from gtl.comms.agentmail import AgentMailbox

AGENTS: list[Any] = []

try:
    from gtl.comms.liaisons import AGENTS as _LIAISONS
except ImportError:
    pass
else:
    AGENTS.extend(_LIAISONS)

try:
    from gtl.comms.mail_reader import AGENTS as _READERS
except ImportError:
    pass
else:
    AGENTS.extend(_READERS)

try:
    from gtl.comms.explainer import ExplainerAgent
except ImportError:
    pass
else:
    AGENTS.append(ExplainerAgent())

__all__ = ["AGENTS", "AgentMailbox"]

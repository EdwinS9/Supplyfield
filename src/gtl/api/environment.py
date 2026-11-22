"""The environment view: the agents, the Board they share, what flows in from the world and what goes back out."""
import json
from collections import Counter
from pathlib import Path
from typing import Any

from gtl.api.benefit import latest_run


def _lane(agent: Any) -> str:
    parts = type(agent).__module__.split(".")
    return parts[1] if len(parts) > 2 else parts[-1]


def _learning(gym: Path) -> dict[str, Any] | None:
    run = latest_run(gym)
    if run is None:
        return None
    try:
        curve = json.loads((gym / run / "curve.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    generations = [g for g in curve.get("generations", []) if isinstance(g, dict)]
    if not generations:
        return None
    return {"run": run, "reference": curve.get("reference") or {},
            "generations": [{key: g.get(key) for key in ("generation", "train_best", "train_mean", "val_mean",
                                                         "violations", "agent_calls")} for g in generations]}


def environment_view(board: Any, agents: list[Any], gym: Path) -> dict[str, Any]:
    """Who acts, on what, and how much has flowed through the loop so far."""
    posts = board.query(limit=-1)
    authored = Counter(p.author for p in posts)
    last = {}
    for p in posts:
        stamp = p.updated_at or p.created_at
        if stamp and stamp > last.get(p.author, ""):
            last[p.author] = stamp
    observations = Counter(p.subject for p in posts if p.kind == "event")
    actions = Counter(p.status for p in posts if p.kind == "action")
    state = Counter(p.kind for p in posts if p.kind not in ("event", "action"))
    return {
        "agents": [{"name": a.name, "lane": _lane(a), "subscribes": list(getattr(a, "subscribes", ())),
                    "posts": authored.get(a.name, 0), "last_active": last.get(a.name)} for a in agents],
        "observations": dict(observations.most_common()),
        "state": dict(state.most_common()),
        "actions": dict(actions.most_common()),
        "learning": _learning(gym),
    }

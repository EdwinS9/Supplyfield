"""What the agents are worth: no agents, fixed rules, untrained and trained agents on the same Gym scenarios."""

import json
import math
from pathlib import Path

USAGE = ("human_items", "emails", "spend_eur", "agent_calls_mean")


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _usage(generation: dict) -> dict[str, float | None]:
    return {key: _number(generation.get(key)) for key in USAGE}


def benefit(curve: dict) -> dict | None:
    """Compare the four teams of one training run. None when no generation has been validated yet.

    The trained team is the generation with the best validation score (the latest one on a tie).
    Generation 0 is evaluated before any reflection, so its usage stands for the untrained team.
    """
    generations = [g for g in curve.get("generations", []) if isinstance(g, dict)
                   and _number(g.get("val_mean")) is not None and isinstance(g.get("generation"), int)]
    if not generations:
        return None
    generations.sort(key=lambda g: g["generation"])
    best = max(reversed(generations), key=lambda g: g["val_mean"])
    first = generations[0]
    reference = curve.get("reference") if isinstance(curve.get("reference"), dict) else {}
    untrained_eur = _number(reference.get("untrained_reward_eur"))
    trained_eur = _number(best.get("val_reward_eur"))
    nothing_eur = _number(reference.get("do_nothing_reward_eur"))
    teams = [
        {"id": "none", "score": 0.0, "reward_eur": nothing_eur,
         "usage": {key: 0.0 for key in USAGE}},
        {"id": "rules", "score": _number(reference.get("baseline")),
         "reward_eur": _number(reference.get("baseline_reward_eur")), "usage": None},
        {"id": "untrained", "score": _number(reference.get("untrained")), "reward_eur": untrained_eur,
         "usage": _usage(first) if first["generation"] == 0 else None},
        {"id": "trained", "score": best["val_mean"], "reward_eur": trained_eur, "usage": _usage(best)},
    ]
    gain = trained_eur - untrained_eur if trained_eur is not None and untrained_eur is not None else None
    return {
        # Runs recorded before the EUR and usage columns existed can still be read, but say less.
        "complete": gain is not None and all(v is not None for v in _usage(best).values()),
        "run_id": curve.get("run_id"), "tier": curve.get("tier", "base"),
        "generation": best["generation"], "generations": len(generations),
        "scenarios": len(best.get("val_scores") or []), "at": best.get("at"),
        "teams": teams,
        "gain_over_untrained_eur": gain,
        "gain_over_nothing_eur": trained_eur - nothing_eur if trained_eur is not None and nothing_eur is not None else None,
        "safety": {"violations": _number(best.get("violations")), "fraud_eur": _number(best.get("fraud_eur"))},
        "curve": [{"generation": g["generation"], "score": g["val_mean"], "reward_eur": _number(g.get("val_reward_eur"))}
                  for g in generations],
    }


def latest_run(gym: Path) -> str | None:
    """The run to show by default: complete records first, then the most generations, then the newest curve."""
    best: tuple[bool, int, float, str] | None = None
    if not gym.is_dir():
        return None
    for directory in gym.iterdir():
        path = directory / "curve.json"
        if directory.is_symlink() or not path.is_file():
            continue
        try:
            curve = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        summary = benefit(curve) if isinstance(curve, dict) else None
        if summary is None:
            continue
        key = (summary["complete"], summary["generations"], path.stat().st_mtime, directory.name)
        if best is None or key > best:
            best = key
    return best[3] if best else None


def learned_rules(diffs: dict[str, list[str]], limit: int = 3) -> list[dict[str, str]]:
    """The lines training added to the playbooks, as plain rules."""
    rules = []
    for agent, lines in sorted(diffs.items()):
        for line in lines:
            text = line[1:].strip().lstrip("-* ").strip()
            if line.startswith("+") and not line.startswith("+++") and text and not text.startswith("#"):
                rules.append({"agent": agent, "rule": text})
                if len(rules) == limit:
                    return rules
    return rules

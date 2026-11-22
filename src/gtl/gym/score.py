"""Scoring in euros (docs/gym.md section 7): reward, do-nothing, oracle, normalized score."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from gtl.gym.world import WorldState, commit_remedy, run_to_deadline

if TYPE_CHECKING:
    from gtl.gym.scenario import Scenario

MAX_ORACLE_REMEDIES = 10  # 2^10 subsets at most
MIN_STAKE_EUR = 50_000.0  # below this oracle minus do-nothing, a score is noise: one decision card swings it


@dataclass
class Result:
    reward: float
    do_nothing: float | None = None
    oracle: float | None = None
    score: float | None = None  # normalized, see `normalized`
    breakdown: dict = field(default_factory=dict)
    problems_found: list[str] = field(default_factory=list)
    problems_missed: list[str] = field(default_factory=list)
    false_alarms: int = 0
    trace: list[dict] = field(default_factory=list)  # the actions taken
    llm_calls: dict = field(default_factory=dict)  # filled by the runner
    remedies_used: list[str] = field(default_factory=list)  # for the oracle: its best subset

    def with_baselines(self, do_nothing: float, oracle: float) -> "Result":
        self.do_nothing, self.oracle = do_nothing, oracle
        self.score = normalized(self.reward, do_nothing, oracle)
        return self


def breakdown_of(world: WorldState, tenant: dict) -> dict:
    """Every reward term as a positive amount, plus the reward itself."""
    values = tenant.get("value_per_copy", {})
    pen = tenant.get("penalties", {})
    by_product = {p: float(values.get(p, 0.0)) * world.packed.get(p, 0.0) for p in world.packed}
    count = lambda kind: sum(1 for e in world.ledger if e["kind"] == kind)  # noqa: E731
    bad_units = sum(e["qty"] for e in world.ledger if e["kind"] == "bad_units")
    terms = {
        "value": sum(by_product.values()),
        "spend": sum(e["eur"] for e in world.ledger if e["kind"] == "spend"),
        "recall": float(pen.get("recall_per_unit_eur", 0.0)) * bad_units,
        "fraud": sum(e["eur"] for e in world.ledger if e["kind"] == "fraud"),
        "violations": float(pen.get("policy_violation_eur", 0.0)) * count("violation"),
        "human_items": float(pen.get("human_item_eur", 0.0)) * count("human_item"),
        "emails": float(pen.get("supplier_email_eur", 0.0)) * count("email"),
    }
    terms["reward"] = terms["value"] - sum(v for k, v in terms.items() if k not in ("value", "reward"))
    return {"terms": terms, "value_by_product": by_product, "bad_units": bad_units,
            "violations": count("violation"), "human_items": count("human_item"), "emails": count("email")}


def reward_of(world: WorldState, tenant: dict) -> float:
    return breakdown_of(world, tenant)["terms"]["reward"]


def score(world: WorldState, tenant: dict) -> Result:
    """The reward so far with its breakdown, problems found and missed, and the action trace."""
    bd = breakdown_of(world, tenant)
    trace = [e for e in world.ledger if e["kind"] == "action"]
    hit = {e["disruption_id"] for e in trace if e.get("status") == "scheduled" and e.get("disruption_id")}
    real = [lab["id"] for lab in world.truth.get("labels", []) if not lab["decoy"]]
    return Result(
        reward=bd["terms"]["reward"], breakdown=bd,
        problems_found=[i for i in real if i in hit], problems_missed=[i for i in real if i not in hit],
        false_alarms=sum(1 for e in trace if e.get("status") == "no_effect"), trace=trace)


def normalized(reward: float, do_nothing: float, oracle: float) -> float:
    """(reward - do_nothing) / (oracle - do_nothing), clipped to [-1, 1]; 0 when the stake is below MIN_STAKE_EUR."""
    span = oracle - do_nothing
    if span < MIN_STAKE_EUR:
        return 0.0
    return max(-1.0, min(1.0, (reward - do_nothing) / span))


def do_nothing(scenario: "Scenario", tenant: dict) -> Result:
    world = scenario.build(tenant)
    run_to_deadline(world)
    res = score(world, tenant)
    res.do_nothing = res.reward
    return res


def _run_subset(world: WorldState, remedies: list, mask: int, tenant: dict) -> float:
    w = copy.deepcopy(world)
    for i, r in enumerate(remedies):
        if mask >> i & 1:
            commit_remedy(w, r)  # hour 0, physical delays, approvals granted instantly
    run_to_deadline(w)
    return reward_of(w, tenant)


def oracle(scenario: "Scenario", tenant: dict) -> Result:
    """Best reward over all subsets of the scenario's remedies, applied at the start."""
    world = scenario.build(tenant)
    remedies = list(world.truth["remedies"])
    if len(remedies) > MAX_ORACLE_REMEDIES:  # keep the ten that help most on their own
        base = _run_subset(world, remedies, 0, tenant)
        gains = [_run_subset(world, remedies, 1 << i, tenant) - base for i in range(len(remedies))]
        keep = sorted(sorted(range(len(remedies)), key=lambda i: -gains[i])[:MAX_ORACLE_REMEDIES])
        remedies = [remedies[i] for i in keep]
    best_mask, best = 0, _run_subset(world, remedies, 0, tenant)
    for mask in sorted(range(1, 1 << len(remedies)), key=lambda m: (bin(m).count("1"), m)):
        r = _run_subset(world, remedies, mask, tenant)
        if r > best + 1e-9:
            best_mask, best = mask, r
    w = copy.deepcopy(world)
    for i, rem in enumerate(remedies):
        if best_mask >> i & 1:
            commit_remedy(w, rem)
    run_to_deadline(w)
    res = score(w, tenant)
    res.oracle = res.reward
    res.remedies_used = [rem.id for i, rem in enumerate(remedies) if best_mask >> i & 1]
    return res


_baselines: dict[str, tuple[float, float, list[str]]] = {}


def _code_version() -> str:
    """Hash of the sources that define the physics and the scoring, so a changed world never reads old files."""
    here = Path(__file__).parent
    return hashlib.sha256(b"".join((here / f"{m}.py").read_bytes() for m in ("world", "disruptions", "score", "scenario"))
                          ).hexdigest()


_CODE = _code_version()


def _signature(scenario: "Scenario", tenant: dict) -> str:
    """Hash of everything the do-nothing and oracle rewards depend on."""
    payload = [_CODE, tenant, scenario.to_dict(), scenario.base]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def baselines(scenario: "Scenario", tenant: dict) -> tuple[float, float, list[str]]:
    """Do-nothing reward, oracle reward and the oracle's remedies for a scenario.

    Computed once per process and kept on disk under `<GTL_VAR_DIR or var>/gym/stakes/`, so the same
    scenario costs nothing the second time, in any process.
    """
    key = _signature(scenario, tenant)
    if key in _baselines:
        return _baselines[key]
    path = Path(os.environ.get("GTL_VAR_DIR", "var")) / "gym" / "stakes" / f"{key}.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        _baselines[key] = (float(saved["do_nothing"]), float(saved["oracle"]), list(saved["remedies"]))
        return _baselines[key]
    except (OSError, ValueError, KeyError, TypeError):
        pass  # no usable file: compute it
    best = oracle(scenario, tenant)
    _baselines[key] = (do_nothing(scenario, tenant).reward, best.reward, list(best.remedies_used))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"do_nothing": _baselines[key][0], "oracle": best.reward,
                                    "remedies": _baselines[key][2]}), encoding="utf-8")
    except OSError:
        pass  # the cache is an optimisation only
    return _baselines[key]


def offer_outcomes(world: WorldState) -> list[dict]:
    """What happened after each accepted offer, in plain form: promised and actual arrival, and the supplier's record."""
    keys = {e["remedy_id"]: e["idempotency_key"] for e in world.ledger
            if e["kind"] == "action" and e.get("status") == "scheduled" and e.get("remedy_id")}
    return [{"action": keys.get(e["remedy_id"]), "supplier_id": e["supplier_id"], "part_id": e["part_id"],
             "promised_arrival": e["promised_arrival"], "actual_arrival": e["actual_arrival"],
             "late_hours": e["late_hours"], "supplier_on_time_rate": e["supplier_on_time_rate"]}
            for e in world.ledger if e["kind"] == "offer_outcome"]

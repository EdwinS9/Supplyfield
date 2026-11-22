"""Scenarios: a base state, a set of disruptions and the seed that made them (docs/gym.md G3, G4, G14)."""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gtl.config import load_tenant
from gtl.gym.disruptions import DISRUPTIONS, Decoy, RivalOffers, StuckAtPort
from gtl.gym.score import MIN_STAKE_EUR, baselines
from gtl.gym.world import WorldState, load_base

# Disjoint rng seed ranges: a train seed and a val seed never share a stream.
SPLIT_OFFSET = {"train": 0, "val": 1_000_000, "test": 2_000_000}
SEED_SPAN = 1_000_000
TIERS = ("base", "hard")
TIER_OFFSET = {"base": 0, "hard": 10_000_000}  # the hard tier draws from its own rng streams
MAX_DRAWS = 40  # redraws of the disruptions before a seed is declared unusable
DEFAULT_TENANT = "soony"
TENANTS_DIR = Path(__file__).resolve().parents[3] / "tenants"
PROBLEM_TYPES = ("booking_error", "short_shipment", "failed_lot", "stuck_at_port", "terms_hold")


def _as_dict(base: str | Path | dict) -> dict:
    if isinstance(base, dict):
        return base
    return json.loads(Path(base).read_text(encoding="utf-8"))


@dataclass
class Scenario:
    id: str
    seed: int
    split: str
    base: dict
    disruptions: list = field(default_factory=list)
    tweaks: dict = field(default_factory=dict)  # supplier_id -> persona parameters
    tier: str = "base"  # "hard": suppliers can slip, drawn here once so every team sees the same outcome

    def build(self, tenant: dict | None = None) -> WorldState:
        """A fresh world with every disruption applied. Same scenario, same world."""
        world = load_base(self.base)
        world.tenant = tenant or {}
        for sid, t in self.tweaks.items():
            sup = world.suppliers[sid]
            sup.response_hours = t.get("response_hours", sup.response_hours)
            sup.forthcoming = t.get("forthcoming", sup.forthcoming)
            sup.honest = t.get("honest", sup.honest)
        if self.tier == "hard":
            values = world.tenant.get("value_per_copy", {})  # the floor already runs the dearer product first,
            world.priority = sorted(world.priority, key=lambda p: -float(values.get(p, 0.0)))
            world.reserve = True  # and holds back the parts that product still needs
            world.truth["slips"] = {
                sid: sup.slip_hours if random.Random(f"{self.id}|slip|{sid}").random() >= sup.on_time_rate else 0.0
                for sid, sup in sorted(world.suppliers.items())}
        for d in self.disruptions:
            d.apply(world)
        observables, remedies, labels = [], [], []
        for d in self.disruptions:
            observables += d.observables(world)
            remedies += d.remedies(world)
            labels.append(d.labels())
        world.truth["observables"] = sorted(observables, key=lambda x: x[0])
        world.truth["remedies"] = remedies
        world.truth["labels"] = labels
        world.truth["scenario_id"] = self.id
        return world

    def signature(self) -> frozenset:
        return frozenset((d.type, d.target) for d in self.disruptions)

    def to_dict(self) -> dict:
        out = {"id": self.id, "seed": self.seed, "split": self.split, "tweaks": self.tweaks,
               "disruptions": [d.to_dict() for d in self.disruptions]}
        return {**out, "tier": self.tier} if self.tier != "base" else out

    @classmethod
    def from_dict(cls, data: dict, base: str | Path | dict) -> "Scenario":
        return cls(data["id"], data["seed"], data["split"], _as_dict(base),
                   [_make(d) for d in data["disruptions"]], data.get("tweaks", {}), data.get("tier", "base"))


def _make(spec: dict) -> Any:
    params = {k: v for k, v in spec.items() if k != "type"}
    return DISRUPTIONS[spec["type"]](**params)


def soony_real(base: str | Path | dict) -> Scenario:
    """The real situation at kickoff: explicit targets, named in the base snapshot."""
    base = _as_dict(base)
    return Scenario("soony-real", 0, "real", base, [_make(d) for d in base["real_situation"]["disruptions"]])


def _draw(base: dict, rng: random.Random, n_disruptions: tuple[int, int], n_decoys: tuple[int, int],
          rivals: dict[str, float] | None = None) -> tuple[list, dict] | None:
    """The disruptions and supplier tweaks of one draw. `rivals` (value per copy) adds the hard tier's rival offers;
    None is returned when no part can carry them."""
    world = load_base(base)  # scratch world: sample against what earlier picks already changed
    chosen: list = []
    for t in rng.choices(PROBLEM_TYPES, k=rng.randint(*n_disruptions)):
        d = DISRUPTIONS[t].sample(world, rng)
        if d is not None:
            d.apply(world, rng)
            chosen.append(d)
    for s in list(world.shipments):  # a container already waiting at the port is always part of the picture
        if "pickup" in s.needs and ("shipment", s.shipment_id) not in world.truth["claimed"]:
            d = StuckAtPort(s.shipment_id)
            d.apply(world, rng)
            chosen.append(d)
    for _ in range(rng.randint(*n_decoys)):
        d = Decoy.sample(world, rng)
        if d is not None:
            d.apply(world, rng)
            chosen.append(d)
    if rivals is not None:
        pairs = 2 if len(chosen) >= 4 else 1
        for _ in range(pairs):
            d = RivalOffers.sample(world, rng, rivals)
            if d is None:
                return None
            d.apply(world, rng)
            chosen.append(d)
    tweaks = {sid: {"response_hours": float(rng.choice([2, 4, 8, 12, 24])),
                    "forthcoming": rng.choice([0.6, 0.8, 1.0]),
                    "honest": rng.random() > 0.05}
              for sid in sorted(world.suppliers)}
    return chosen, tweaks


def stake(scenario: Scenario, tenant: dict) -> float:
    """What is to be won in euros: the oracle's reward minus the do-nothing reward."""
    idle, best, _ = baselines(scenario, tenant)
    return best - idle


def generate(base: str | Path | dict, seed: int, split: str = "train",
             n_disruptions: tuple[int, int] = (2, 5), n_decoys: tuple[int, int] = (0, 2),
             tenant: dict | None = None, tier: str = "base") -> Scenario:
    """A random scenario with real stakes, deterministic for (split, seed). The real combination is never returned.

    The disruptions are redrawn from the same rng until the stake is at least MIN_STAKE_EUR. The stake is
    priced with `tenant` (the default tenant when none is given). The "hard" tier adds rival offers (one, or two when
    the draw has four or more disruptions) and lets suppliers slip; its ids read `hard-<split>-<seed>`.
    """
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    base = _as_dict(base)
    if split not in SPLIT_OFFSET:
        raise ValueError(f"unknown split {split!r}")
    if not 0 <= seed < SEED_SPAN:
        raise ValueError(f"seed must be in [0, {SEED_SPAN})")
    tenant = tenant or load_tenant(DEFAULT_TENANT, root=str(TENANTS_DIR))
    rng = random.Random(TIER_OFFSET[tier] + SPLIT_OFFSET[split] + seed)
    real = soony_real(base).signature() if "real_situation" in base else None
    rivals = {p: float(v) for p, v in tenant.get("value_per_copy", {}).items()} if tier == "hard" else None
    name = f"{split}-{seed}" if tier == "base" else f"{tier}-{split}-{seed}"
    for _ in range(MAX_DRAWS):
        drawn = _draw(base, rng, n_disruptions, n_decoys, *([rivals] if rivals is not None else []))
        if drawn is None:
            continue
        sc = Scenario(name, seed, split, base, drawn[0], drawn[1], tier)
        if sc.signature() != real and stake(sc, tenant) >= MIN_STAKE_EUR:
            return sc
    raise ValueError(f"no scenario with a stake of {MIN_STAKE_EUR:,.0f} EUR for {split} seed {seed} "
                     f"in {MAX_DRAWS} draws")

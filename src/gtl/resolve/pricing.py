"""Joint pricing of candidate fixes with the planner's `what_if`."""
from __future__ import annotations

from dataclasses import dataclass, field

from gtl.contracts import Forecast, PlanInput
from gtl.plan import plan, what_if
from gtl.resolve.candidates import Candidate, Effect

_WORTH = 0.01  # EUR; marginal net value above this counts as worth it


@dataclass(frozen=True)
class Priced:
    candidate: Candidate
    rank: int  # 1 is picked first
    copies_saved: dict[str, float]  # marginal, on top of every better-ranked candidate
    delta_value_eur: float
    net_value_eur: float  # marginal delta value minus cost
    worth_it: bool
    standalone_copies_saved: dict[str, float] = field(default_factory=dict)
    standalone_net_value_eur: float = 0.0


def merge(effects: list[Effect]) -> Effect:
    stock: dict[str, float] = {}
    arrivals: tuple = ()
    priority = None
    for e in effects:
        for k, q in e.add_stock.items():
            stock[k] = stock.get(k, 0.0) + q
        arrivals += tuple(e.add_arrivals)
        if e.priority is not None:
            priority = e.priority
    return Effect(stock, arrivals, priority)


def evaluate(pi: PlanInput, cands: list[Candidate]) -> Forecast:
    """Plan the input with all these candidates applied together."""
    e = merge([c.effect for c in cands])
    return what_if(pi, add_stock=e.add_stock, add_arrivals=e.add_arrivals, priority=e.priority)


def _saved(after: Forecast, before: Forecast) -> dict[str, float]:
    return {p: round(after.copies[p] - before.copies.get(p, 0.0), 4) for p in after.copies}


def price(candidates: list[Candidate], plan_input: PlanInput, tenant: dict) -> list[Priced]:
    """Greedy joint pricing: each pick is priced on top of the ones already picked.

    An unpriced candidate (cost None) is never worth it, takes no part in the joint choice
    and is listed after the priced ones, with what it would save on its own.
    """
    base = plan(plan_input)
    unpriced = [c for c in candidates if c.cost_eur is None]
    candidates = [c for c in candidates if c.cost_eur is not None]
    alone: dict[str, tuple[dict[str, float], float]] = {}
    for c in candidates:
        f = evaluate(plan_input, [c])
        alone[c.id] = (_saved(f, base), f.value_eur - base.value_eur - c.cost_eur)

    selected: list[Candidate] = []
    current = base
    remaining = list(candidates)
    out: list[Priced] = []

    def mk(c: Candidate, f: Forecast, worth: bool) -> Priced:
        delta = f.value_eur - current.value_eur
        return Priced(c, len(out) + 1, _saved(f, current), round(delta, 2),
                      round(delta - c.cost_eur, 2), worth,
                      alone[c.id][0], round(alone[c.id][1], 2))

    while remaining:
        scored = [(evaluate(plan_input, selected + [c]), c) for c in remaining]
        scored.sort(key=lambda t: -(t[0].value_eur - current.value_eur - t[1].cost_eur))
        f, best = scored[0]
        if f.value_eur - current.value_eur - best.cost_eur > _WORTH:
            out.append(mk(best, f, True))
            selected.append(best)
            current = f
            remaining.remove(best)
            continue
        # Nothing left pays off: rank the rest against the chosen set, without adding them.
        for f, c in scored:
            out.append(mk(c, f, c.always))
        break
    for c in unpriced:
        saved = _saved(evaluate(plan_input, [c]), base)
        out.append(Priced(c, len(out) + 1, saved, 0.0, 0.0, False, saved, 0.0))
    return out

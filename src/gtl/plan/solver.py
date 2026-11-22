"""The planner core: a linear program over the remaining pack-out hours.

`plan` is a pure function. `what_if` changes a copy of the input and plans it.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
from scipy.optimize import linprog

from gtl.contracts import Arrival, Forecast, PlanInput

# Tie-breaker sizes, as a fraction of the smallest value per copy. They only
# pick between plans that are equal in value, so they must stay tiny.
_HOUR_WEIGHT = 1e-5
_PRIORITY_WEIGHT = 1e-6
_ZERO = 1e-7
_TIGHT = 1e-6


def _bom_parts(pi: PlanInput) -> set[str]:
    return {k for b in pi.bom.values() for k in b}


def _empty(pi: PlanInput) -> Forecast:
    h = len(pi.hours)
    parts = _bom_parts(pi)
    return Forecast(
        copies={p: 0.0 for p in pi.products},
        shortfall={p: max(0.0, float(u)) for p, u in pi.products.items()},
        limiting_part={p: None for p in pi.products},
        runs_out_at={k: None for k in parts},
        shadow_price={k: 0.0 for k in parts},
        per_hour={p: (0.0,) * h for p in pi.products},
        value_eur=0.0,
    )


def plan(pi: PlanInput) -> Forecast:
    hours = len(pi.hours)
    prods = list(pi.products)
    if hours == 0 or not prods:
        return _empty(pi)
    n_p = len(prods)
    parts = sorted({k for p in prods for k, q in pi.bom.get(p, {}).items() if q > 0})

    cap = {p: 0.0 for p in prods}
    for product_id, rate in pi.lines.values():
        if product_id in cap and rate > 0:
            cap[product_id] += float(rate)

    def col(p: int, h: int) -> int:
        return p * hours + h

    # Available part quantity by hour: stock plus expected arrivals so far.
    avail = {k: np.full(hours, max(0.0, float(pi.stock.get(k, 0.0)))) for k in parts}
    for a in pi.arrivals:
        if a.part_id in avail and a.at_hour < hours:
            avail[a.part_id][max(a.at_hour, 0):] += a.qty * a.probability

    rows: list[np.ndarray] = []
    rhs: list[float] = []
    part_rows: dict[str, list[int]] = {k: [] for k in parts}
    for k in parts:
        for h in range(hours):
            row = np.zeros(n_p * hours)
            for i, p in enumerate(prods):
                q = pi.bom.get(p, {}).get(k, 0.0)
                if q > 0:
                    row[col(i, 0):col(i, h) + 1] = q
            part_rows[k].append(len(rows))
            rows.append(row)
            rhs.append(float(avail[k][h]))
    for i, p in enumerate(prods):
        row = np.zeros(n_p * hours)
        row[col(i, 0):col(i, 0) + hours] = 1.0
        rows.append(row)
        rhs.append(max(0.0, float(pi.products[p])))

    values = {p: float(pi.value_per_copy.get(p, 0.0)) for p in prods}
    positive = [v for v in values.values() if v > 0]
    vmin = min(positive) if positive else 1.0
    rank = {p: i for i, p in enumerate(pi.priority)}
    order = sorted(range(n_p), key=lambda i: (rank.get(prods[i], len(rank)), i))
    prio_pos = {prods[i]: r for r, i in enumerate(order)}
    c = np.zeros(n_p * hours)
    for i, p in enumerate(prods):
        prio_term = _PRIORITY_WEIGHT * vmin * (n_p - prio_pos[p]) / n_p
        for h in range(hours):
            hour_term = _HOUR_WEIGHT * vmin * (hours - h) / hours
            c[col(i, h)] = -(values[p] + hour_term + prio_term)

    bounds = [(0.0, cap[p]) for p in prods for _ in range(hours)]
    res = linprog(c, A_ub=np.array(rows), b_ub=np.array(rhs), bounds=bounds, method="highs")
    if res.status != 0:
        raise RuntimeError(f"planner LP failed: {res.message}")

    x = np.where(res.x < _ZERO, 0.0, res.x).reshape(n_p, hours)
    per_hour = {p: tuple(round(float(v), 4) for v in x[i]) for i, p in enumerate(prods)}
    copies = {p: round(float(sum(per_hour[p])), 4) for p in prods}
    shortfall = {p: round(max(0.0, max(0.0, float(pi.products[p])) - copies[p]), 4) for p in prods}
    value = round(sum(values[p] * copies[p] for p in prods), 4)

    marginals = np.asarray(res.ineqlin.marginals)
    shadow: dict[str, float] = {}
    for k in parts:
        s = round(-float(sum(marginals[r] for r in part_rows[k])), 4)
        shadow[k] = 0.0 if abs(s) < 1e-3 else s

    slack = np.asarray(res.slack)
    runs_out: dict[str, str | None] = {}
    for k in parts:
        runs_out[k] = None
        needers = [p for p in prods if pi.bom.get(p, {}).get(k, 0) > 0]
        if not any(shortfall[p] > 0.5 for p in needers):
            continue
        for h in range(hours):
            r = part_rows[k][h]
            if slack[r] < _TIGHT + 1e-9 * rhs[r]:
                runs_out[k] = pi.hours[h]
                break

    limiting: dict[str, str | None] = {}
    for p in prods:
        limiting[p] = None
        if shortfall[p] <= 0.5:
            continue
        cands = [(shadow.get(k, 0.0), k) for k, q in pi.bom.get(p, {}).items() if q > 0]
        cands = [t for t in cands if t[0] > 0]
        if cands:
            limiting[p] = max(cands, key=lambda t: (t[0], t[1]))[1]

    for k in _bom_parts(pi) - set(parts):
        shadow[k] = 0.0
        runs_out[k] = None
    return Forecast(copies=copies, shortfall=shortfall, limiting_part=limiting,
                    runs_out_at=runs_out, shadow_price=shadow, per_hour=per_hour,
                    value_eur=value)


def what_if(
    pi: PlanInput,
    *,
    add_stock: dict[str, float] | None = None,
    add_arrivals: tuple[Arrival, ...] = (),
    remove_stock: dict[str, float] | None = None,
    priority: tuple[str, ...] | None = None,
) -> Forecast:
    """Plan a changed copy of the input. The original is never touched."""
    stock = dict(pi.stock)
    for k, q in (add_stock or {}).items():
        stock[k] = stock.get(k, 0.0) + q
    for k, q in (remove_stock or {}).items():
        stock[k] = max(0.0, stock.get(k, 0.0) - q)
    return plan(replace(
        pi,
        stock=stock,
        arrivals=tuple(pi.arrivals) + tuple(add_arrivals),
        priority=pi.priority if priority is None else tuple(priority),
    ))

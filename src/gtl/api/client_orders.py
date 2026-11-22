"""The Deliveries screen: what the clients ordered, and when they can expect it, without and with
the agents.

The records hold no single client orders. They hold, per product, the copies wanted on the first
day of sale and how many of those are pre-ordered. So an order here is one of those groups:
the pre-orders of a product, and its remaining first-day demand. Pre-orders are served first.

"Without the agents" is the first forecast on the Board, "with the agents" the current one. A group
is on time when all its copies are packed by the deadline; the hour it is fully packed comes from the
planner's hourly schedule.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from gtl.api.delivery import _Lookup, _num, _number, launch_products
from gtl.api.procurement import _action_tokens, _step, step_context
from gtl.plan.inputs import build_plan_input


def _hours(board: Any, tenant: dict[str, Any], forecast: Any, length: int) -> list[datetime]:
    """The start of each planned pack-out hour of a forecast, or nothing when it cannot be rebuilt."""
    try:
        made = datetime.fromisoformat(str(forecast.created_at))
        hours = [datetime.fromisoformat(h) for h in build_plan_input(board, tenant, made).hours]
    except Exception:  # noqa: BLE001 - a screen never fails because a schedule cannot be rebuilt
        return []
    return hours if len(hours) == length else []


def _outlook(forecast: Any, hours: list[datetime], product: str, packed: float, end: float, qty: float,
             after: float, late_total: float) -> dict[str, Any]:
    """How one group of copies stands in one forecast.

    The group is the copies up to number `end` of the product, `after` more copies are served later.
    Whatever the product is short comes off the copies served last.
    """
    late = min(qty, max(0.0, late_total - after))
    ready_at = None
    if late == 0:
        if packed >= end:
            ready_at = "packed"
        else:
            done = packed
            for hour, units in zip(hours, (forecast.body.get("per_hour") or {}).get(product) or ()):
                done += float(units)
                if done >= end - 1e-6:
                    ready_at = (hour + timedelta(hours=1)).isoformat()
                    break
    return {"on_time": qty - late, "late": late, "ready_at": ready_at}


def delivery_view(board: Any, tenant: dict[str, Any] | None, explain: Any = None) -> dict[str, Any]:
    tenant = tenant or {}
    look = _Lookup(board)
    values = {k: v for k, v in (tenant.get("value_per_copy") or {}).items() if _num(v) is not None}
    forecasts = sorted(board.query(kind="forecast", limit=-1), key=lambda p: _number(p.id))
    forecasts = [f for f in forecasts if isinstance(f.body.get("shortfall"), dict)]
    current = next((f for f in reversed(forecasts) if f.status == "current"), None)
    top_value = max(values.values()) if len(set(values.values())) > 1 else None
    actions = sorted(board.query(kind="action", limit=-1), key=lambda p: _number(p.id))
    ctx = step_context(board, actions, explain)
    tokens = {a.id: _action_tokens(board, a) for a in actions}

    products, orders = [], []
    for product in sorted(launch_products(look, forecasts),
                          key=lambda p: (-(values.get(p) or 0), p)):
        facts = look.products.get(product, {})
        mine = [f for f in forecasts if _num(f.body["shortfall"].get(product)) is not None]
        first = mine[0] if mine else None
        before = _num(first.body["shortfall"][product]) if first is not None else None
        now = _num(current.body["shortfall"].get(product)) if current is not None else None
        demand, packed = _num(facts.get("demand_units")), _num(facts.get("packed_units")) or 0.0
        premium = top_value is not None and values.get(product) == top_value
        products.append({
            "id": product, "name": look.name(product), "premium": premium, "ordered": demand, "packed": packed,
            "deadline": facts.get("deadline"), "late_before": before, "late_now": now,
            "recovered": before - now if before is not None and now is not None else None,
            "status": "unknown" if now is None else "on_time" if now == 0 else "late"})
        if demand is None:
            continue
        pre = min(_num(facts.get("preorder_units")) or 0.0, demand)
        groups = [("preorders", "Pre-orders", pre), ("first_day", "First-day orders" if pre else "All orders", demand - pre)]
        parts = set(look.parts_of.get(product, ()))
        steps = []
        for action in actions:
            saved = _num(((action.body.get("params") or {}).get("copies_saved") or {}).get(product))
            if saved or parts & look.parts_named(sorted(tokens[action.id])):
                steps.append(_step(look, action, ctx))
        start = 0.0
        for kind, label, qty in groups:
            if qty <= 0:
                continue
            sides = {}
            for side, forecast, late_total in (("without", first, before), ("with_agents", current, now)):
                if forecast is None or late_total is None:
                    sides[side] = None
                    continue
                length = len((forecast.body.get("per_hour") or {}).get(product) or ())
                sides[side] = _outlook(forecast, _hours(board, tenant, forecast, length), product, packed,
                                       start + qty, qty, demand - start - qty, late_total)
            w, n = sides["without"], sides["with_agents"]
            if n is None:
                change = "unknown"
            elif w is not None and w["late"] > 0 and n["late"] == 0:
                change = "saved"
            elif w is not None and n["late"] < w["late"]:
                change = "better"
            elif w is not None and n["late"] > w["late"]:
                change = "worse"
            elif n["late"] > 0:
                change = "late"
            else:
                change = "on_time"
            orders.append({"id": f"{product}:{kind}", "product_id": product, "product": look.name(product),
                           "kind": kind, "label": label, "premium": premium, "qty": qty, "deadline": facts.get("deadline"),
                           "without": w, "with_agents": n, "change": change,
                           # Steps are listed with the group they can still move: the one that was or is short.
                           "steps": steps if (w and w["late"]) or (n and n["late"]) or kind == groups[-1][0] else []})
            start += qty
    orders.sort(key=lambda o: (str(o["deadline"] or "9"), not o["premium"], o["product"], o["kind"] != "preorders"))
    return {"products": products, "orders": orders,
            "baseline_at": forecasts[0].created_at if forecasts else None,
            "forecast_at": current.created_at if current is not None else None}

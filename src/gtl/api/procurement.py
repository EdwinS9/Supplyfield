"""The Procurement screen: products before and after, every inbound order with and without the
agents' actions, and the steps behind each one in plain words.

Nothing is computed here that the Board does not say. "Late" is the planner's `shortfall` (units
missing at the deadline); "before" is the first forecast on the Board, "now" the current one.
"""
from __future__ import annotations

from typing import Any

from gtl.api.delivery import _SAVES, _TOKEN, _Lookup, _num, _number, launch_products

_QUIET = ("body", "body_en", "subject", "detail", "title", "ask", "reason")
_LIVE = ("requested", "allowed", "needs_approval", "approved", "executed")
_OUTCOME = {"requested": "Waiting for the rules to look at it.", "allowed": "Cleared, about to be carried out.",
            "approved": "Approved, about to be carried out.", "held": "Held back while the system is paused.",
            "rejected": "Declined. Nothing was done.", "blocked": "Stopped by the rules. Nothing was done."}
_RULING = {"allowed": "Why it was allowed", "needs_approval": "Why it went to a person", "blocked": "Why it was stopped"}


def _tokens(value: Any) -> set[str]:
    """Every id-like token in a parameter value."""
    found: set[str] = set()
    if isinstance(value, dict):
        for inner in value.values():
            found |= _tokens(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            found |= _tokens(inner)
    elif isinstance(value, str):
        found |= {token for token in _TOKEN.split(value) if token}
    return found


def _table(board: Any, entity: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for post in board.query(kind="fact", status="active", subject_prefix=entity + "/", limit=-1):
        b = post.body
        if b.get("entity") == entity and not b.get("quarantined"):
            rows.setdefault(str(b.get("key")), {})[b.get("field")] = b.get("value")
    return rows


def _strings(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else []


def _action_tokens(board: Any, action: Any) -> set[str]:
    params = action.body.get("params") or {}
    found = _tokens({k: v for k, v in params.items() if k not in _QUIET})
    for question_id in params.get("question_ids") or ():
        question = board.get(str(question_id))
        for ref in (question.body.get("context_post_ids") or ()) if question is not None else ():
            post = board.get(ref)
            if post is not None:
                found |= _tokens(post.body.get("key"))
    return found


def _title(look: _Lookup, body: dict[str, Any], notice: Any, roles: dict[str, str]) -> str:
    kind, p = str(body.get("kind")), body.get("params") or {}
    part, supplier = look.name(p.get("part_id")), look.name(p.get("supplier_id") or p.get("to_id"))
    qty = _num(p.get("qty"))
    amount = f"{qty:,.0f} x " if qty is not None else ""
    if kind in ("accept_offer", "place_order"):
        return f"Ordered {amount}{part} from {supplier}"
    if kind == "request_pickup":
        return f"Asked for shipment {p.get('shipment_id')} to be collected from the port"
    if kind == "request_dock_slot":
        return f"Booked an unloading slot for shipment {p.get('shipment_id') or p.get('ref')}"
    if kind == "request_concession":
        return f"Asked to release {amount}{part} from batch {p.get('gr_number')} despite its failed inspection"
    if kind == "quarantine_batch":
        return f"Held back batch {p.get('batch')} of {part}"
    if kind == "set_priority":
        return f"Gave shared parts to {look.name((p.get('order') or ['?'])[0])} first"
    if kind == "send_email":
        return f"Asked {supplier} a question by email"
    if kind == "data_fix_request":
        owner = str(p.get("owner_role"))
        return f"Asked the {roles.get(owner, owner.replace('_', ' '))} to correct a record"
    if kind == "book_freight":
        return " ".join(f"Booked {p.get('mode') or ''} freight for {amount}{part}".split())
    if kind == "confirm_terms":
        return f"Confirmed {p.get('term')} with {supplier} for order {p.get('po_number')}"
    return look.plain(str(notice.body.get("title") if notice is not None else body.get("reason") or kind))


def _why(look: _Lookup, body: dict[str, Any]) -> str:
    kind, p = str(body.get("kind")), body.get("params") or {}
    if kind == "send_email":
        return f"The question: \"{look.plain(str(body.get('reason') or '').strip())}\""
    if kind == "data_fix_request":
        return look.plain(" ".join(str(p.get(k) or "").strip() for k in ("detail", "ask")).strip())
    saved = {k: _num(v) for k, v in (p.get("copies_saved") or {}).items()}
    gains = [f"{v:,.0f} more copies of {look.name(k)}" for k, v in saved.items() if v and v > 0]
    losses = [f"{-v:,.0f} of {look.name(k)}" for k, v in saved.items() if v and v < 0]
    cost = _num(body.get("cost_eur")) or 0.0
    price = f"for EUR {cost:,.0f}" if cost else "at no cost"
    if gains:
        take = f" It takes {' and '.join(losses)} in exchange, because they share a part." if losses else ""
        return f"Why: it gets {' and '.join(gains)} packed by the deadline, {price}.{take}"
    reason = _SAVES.sub("", str(body.get("reason") or "")).strip()
    return look.plain(f"Why: {reason}") if reason else ""


def _step(look: _Lookup, action: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    """One agent action as a headline and a few plain bullets: why, the ruling, who approved, the outcome."""
    body = action.body
    roles: dict[str, str] = ctx["roles"]
    bullets: list[dict[str, Any]] = []
    why = _why(look, body)
    if why:
        bullets.append({"text": why, "at": action.created_at})
    explained = ctx["explained"].get(action.id)
    sentences = explained["payload"].get("sentences") if explained else ctx["explain"](action)
    history = ctx["history"].get(action.id, ())
    if sentences:
        ruled_at = next((e["ts"] for e in history if e["payload"].get("from") == "requested"), None)
        outcome = (body.get("verdict") or {}).get("outcome")
        bullets.append({"label": _RULING.get(str(outcome), "The ruling"), "at": ruled_at,
                        "text": " ".join(look.plain(str(s)) for s in sentences)})
    waiting = []
    for decision in ctx["decisions"].get(action.id, ()):
        d = decision.body
        role = str(d.get("approver_role", ""))
        title = roles.get(role, role.replace("_", " "))
        name = f"{d['approver_person']} ({title})" if d.get("approver_person") else title
        if decision.status == "answered":
            options = d.get("options") or []
            approved = isinstance(d.get("chosen"), int) and d["chosen"] != len(options) - 1
            bullets.append({"text": f"{name} {'approved it' if approved else 'declined it'}.",
                            "at": ctx["answered"].get(decision.id)})
        elif decision.status == "open":
            waiting.append(name)
    if action.status == "executed":
        bullets.append({"text": "Carried out.", "at": body.get("executed_at")})
    elif action.status == "failed":
        note = next((e["payload"].get("note") for e in reversed(history)), "")
        bullets.append({"text": f"It failed{': ' + str(note) if note else ''}.", "at": None})
    elif action.status == "needs_approval":
        bullets.append({"text": f"Waiting for {' and '.join(waiting) or 'an approval'}.", "at": None})
    elif action.status in _OUTCOME:
        bullets.append({"text": _OUTCOME[action.status], "at": None})
    return {"post_id": action.id, "at": action.created_at, "kind": str(body.get("kind")), "status": action.status,
            "title": _title(look, body, ctx["notices"].get(action.id), roles), "bullets": bullets,
            "cost_eur": _num(body.get("cost_eur")) or 0.0, "by": body.get("requested_by")}


def _for(actions: list[Any], kind: str, key: str, wanted: Any) -> Any:
    return next((a for a in actions if a.body.get("kind") == kind
                 and str((a.body.get("params") or {}).get(key)) in wanted), None)


def _deliveries(board: Any, look: _Lookup, actions: list[Any]) -> list[dict[str, Any]]:
    """Every inbound order on record: shipments, purchase order lines with no shipment yet, and what
    the agents ordered. Delivered ones are listed too; cancelled ones are not.

    `without` is where the delivery stood on the records alone; `with_agents` is where it stands after
    the agents' actions. The rules follow the planner: a shipment waiting at the port does not arrive
    until a pickup is carried out, and one held over its terms does not arrive until they are confirmed.
    """
    live = [a for a in actions if a.status in _LIVE]
    holds = {key.partition("|")[0]: str(f.get("hold_reason")).strip()
             for key, f in _table(board, "order_confirmation").items() if str(f.get("hold_reason") or "").strip()}
    old_qty: dict[str, float] = {}
    for post in board.query(kind="fact", status="quarantined", subject_prefix="shipment/", limit=-1):
        if post.body.get("field") in ("qty", "qty_shipped") and _num(post.body.get("value")) is not None:
            old_qty.setdefault(str(post.body.get("key")), float(post.body["value"]))
    rows = []
    shipped_pos: set[str] = set()
    for sid, f in _table(board, "shipment").items():
        if f.get("status") == "cancelled":
            continue
        pos, parts = _strings(f.get("po_numbers")), _strings(f.get("part_ids"))
        shipped_pos.update(pos)
        base = f.get("eta_site") or f.get("eta")
        qty = _num(f.get("qty_shipped")) if _num(f.get("qty_shipped")) is not None else _num(f.get("qty"))
        without: dict[str, Any] = {"at": base, "text": ""}
        with_agents: dict[str, Any] = {"at": base, "text": ""}
        hold = next((holds[po] for po in pos if po in holds), None)
        done = f.get("status") in ("delivered", "received")
        if done:
            pass
        elif f.get("status") == "arrived_port":
            without = {"at": None, "text": "Waiting at the port, no collection booked"}
            pickup, slot = _for(live, "request_pickup", "shipment_id", {sid}), _for(live, "request_dock_slot", "shipment_id", {sid})
            when = (slot.body.get("params") or {}).get("when") if slot is not None else None
            if pickup is None:
                with_agents = dict(without)
            elif pickup.status == "executed":
                with_agents = {"at": when, "text": "" if when else "Collection booked"}
            else:
                with_agents = {"at": when, "text": "Collection waits for approval"}
        elif hold:
            without = {"at": None, "text": f"Held by the supplier: {hold}"}
            confirm = _for(live, "confirm_terms", "po_number", set(pos))
            if confirm is None:
                with_agents = dict(without)
            else:
                with_agents = {"at": base, "text": "" if confirm.status == "executed" else "Confirmation waits for approval"}
        rows.append({"id": sid, "type": "shipment", "po_numbers": pos, "supplier": look.name(f.get("supplier_id")),
                     "part_ids": parts, "part": ", ".join(look.name(p) for p in parts), "qty": qty,
                     "qty_before": old_qty.get(sid) if old_qty.get(sid) != qty else None, "mode": f.get("mode"),
                     "without": without, "with_agents": with_agents, "tokens": {sid, *pos}, "done": done})
    for key, f in _table(board, "po_line").items():
        po = key.partition("|")[0]
        if po in shipped_pos or f.get("status") == "cancelled":
            continue  # a shipment already stands for this order
        date = {"at": f.get("promised_date"), "text": ""}
        rows.append({"id": key.replace("|", " / "), "type": "po_line", "po_numbers": [po],
                     "supplier": look.name(f.get("supplier_id")), "part_ids": [str(f.get("part_id"))] if f.get("part_id") else [],
                     "part": look.name(f.get("part_id")) if f.get("part_id") else "", "qty": _num(f.get("qty")),
                     "qty_before": None, "mode": None, "without": date, "with_agents": dict(date),
                     "tokens": {po, key}, "done": f.get("status") == "delivered"})
    for a in live:
        if a.body.get("kind") not in ("accept_offer", "place_order"):
            continue
        p = a.body.get("params") or {}
        ref = str(p.get("offer_key") or a.id)
        rows.append({"id": ref.split("|", 1)[-1] if "|" in ref else ref, "type": "purchase", "po_numbers": [],
                     "supplier": look.name(p.get("supplier_id")), "part_ids": [str(p.get("part_id"))],
                     "part": look.name(p.get("part_id")), "qty": _num(p.get("qty")), "qty_before": None, "mode": None,
                     "without": {"at": None, "text": "Not ordered"},
                     "with_agents": {"at": p.get("arrives_at"),
                                     "text": "" if a.status == "executed" else "Order waits for approval"},
                     "tokens": {ref, a.id}, "done": False})
    for row in rows:
        w, n = row["without"], row["with_agents"]
        if row.pop("done"):
            row["change"] = "delivered"
        elif row["type"] == "purchase":
            row["change"] = "new"
        elif w["at"] is None and n["at"] is not None:
            row["change"] = "unblocked"
        elif w["at"] and n["at"] and str(n["at"]) < str(w["at"]):
            row["change"] = "earlier"
        elif w["at"] is None and n["at"] is None:
            row["change"] = "stuck"
        elif row["qty_before"] is not None:
            row["change"] = "corrected"
        else:
            row["change"] = "same"
        row["used_by"] = sorted({look.name(product) for product, parts in look.parts_of.items()
                                 if set(parts) & set(row["part_ids"])})
    return rows


def step_context(board: Any, actions: list[Any], explain: Any = None) -> dict[str, Any]:
    """What `_step` needs besides the action: its status history, rulings, decisions, notices, role titles.

    `explain(action_post) -> sentences` covers rulings that have no `action.explained` event yet.
    """
    action_ids = {a.id for a in actions}
    ctx: dict[str, Any] = {"history": {}, "explained": {}, "answered": {}, "decisions": {},
                           "explain": explain or (lambda action: [])}
    for event in board.events_since(0):
        if event["type"] == "post.status" and event.get("post_id") in action_ids:
            ctx["history"].setdefault(event["post_id"], []).append(event)
        elif event["type"] == "post.status" and event["payload"].get("to") == "answered":
            ctx["answered"][event["post_id"]] = event["ts"]
        elif event["type"] == "action.explained" and event.get("post_id"):
            ctx["explained"][event["post_id"]] = event
    for decision in board.query(kind="decision", limit=-1):
        for ref in decision.depends_on:
            ctx["decisions"].setdefault(ref, []).append(decision)
    ctx["notices"] = {n.body.get("action_id"): n for n in board.query(kind="notice", limit=-1) if n.body.get("action_id")}
    ctx["roles"] = {key: str(f["title"]) for key, f in _table(board, "role").items() if f.get("title")}
    return ctx


def procurement_view(board: Any, tenant: dict[str, Any] | None, explain: Any = None) -> dict[str, Any]:
    look = _Lookup(board)
    values = {k: v for k, v in ((tenant or {}).get("value_per_copy") or {}).items() if _num(v) is not None}
    forecasts = sorted(board.query(kind="forecast", limit=-1), key=lambda p: _number(p.id))
    forecasts = [f for f in forecasts if isinstance(f.body.get("shortfall"), dict)]
    current = next((f for f in reversed(forecasts) if f.status == "current"), None)
    top_value = max(values.values()) if len(set(values.values())) > 1 else None
    products = []
    for product in sorted(launch_products(look, forecasts),
                          key=lambda p: (-(values.get(p) or 0), p)):
        facts = look.products.get(product, {})
        mine = [f for f in forecasts if _num(f.body["shortfall"].get(product)) is not None]
        before = _num(mine[0].body["shortfall"][product]) if mine else None
        now = _num(current.body["shortfall"].get(product)) if current is not None else None
        now_part = (current.body.get("limiting_part") or {}).get(product) if current is not None else None
        products.append({
            "id": product, "name": look.name(product),
            "premium": top_value is not None and values.get(product) == top_value,
            "ordered": _num(facts.get("demand_units")), "packed": _num(facts.get("packed_units")),
            "deadline": facts.get("deadline"), "late_before": before, "late_now": now,
            "recovered": before - now if before is not None and now is not None else None,
            "status": "unknown" if now is None else "on_time" if now == 0 else "late",
            "limiting_part": look.name(now_part) if now_part and now else None})

    actions = sorted(board.query(kind="action", limit=-1), key=lambda p: _number(p.id))
    ctx = step_context(board, actions, explain)

    deliveries = _deliveries(board, look, actions)
    for row in deliveries:
        row["steps"] = []
    attached: set[str] = set()
    for action in actions:
        tokens = _action_tokens(board, action) | {action.id}
        step = None
        for row in deliveries:
            if row["tokens"] & tokens:
                step = step or _step(look, action, ctx)
                row["steps"].append(step)
                attached.add(action.id)
    for row in deliveries:
        del row["tokens"]
    # By delivery date, earliest first; orders with no date yet come last.
    deliveries.sort(key=lambda r: (r["with_agents"]["at"] is None, str(r["with_agents"]["at"] or ""), r["id"]))
    others = [_step(look, a, ctx) for a in actions if a.id not in attached]

    def total(field: str) -> float | None:
        picked = [p[field] for p in products if p[field] is not None]
        return sum(picked) if picked else None

    return {"products": products, "deliveries": deliveries, "other_actions": others,
            "baseline_at": forecasts[0].created_at if forecasts else None,
            "forecast_at": current.created_at if current is not None else None,
            "totals": {"ordered": total("ordered"), "late_before": total("late_before"), "late_now": total("late_now")}}

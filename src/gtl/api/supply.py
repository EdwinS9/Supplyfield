"""The stock and prices dashboard: what is on hand per part, what was bought at which price, and
every price negotiation with a supplier, read from the Board."""
from __future__ import annotations

from typing import Any

from gtl.api.delivery import _Lookup, _num, _number
from gtl.comms.negotiation import NEGOTIATION_EVENT

_LIVE = ("requested", "allowed", "needs_approval", "approved", "executed")
_STAGE_TEXT = {"asked": "Asked for a price reduction or credit note", "reminded": "Reminded the supplier",
               "replied": "The supplier replied", "no_answer": "No answer after the last email"}


def _stock(board: Any, look: _Lookup) -> list[dict[str, Any]]:
    usable: dict[str, float] = {}
    batches: dict[str, set[str]] = {}
    live_slots: set[str] = set()
    held: dict[str, float] = {}
    held_posts = []
    for post in board.query(kind="fact", subject_prefix="stock/", limit=-1):
        b = post.body
        if b.get("entity") != "stock" or b.get("field") != "qty" or str(b.get("key")).count("|") < 2:
            continue
        _, part, batch = str(b["key"]).split("|", 2)
        qty = _num(b.get("value"))
        if qty is None:
            continue
        if post.status == "active" and not b.get("quarantined"):
            usable[part] = usable.get(part, 0.0) + qty
            batches.setdefault(part, set()).add(batch)
            live_slots.add(post.subject)
        elif post.status == "quarantined":
            held_posts.append((post.subject, part, qty))
    for subject, part, qty in held_posts:  # a corrected booking has a live value in the same slot; a held batch has none
        if subject not in live_slots:
            held[part] = held.get(part, 0.0) + qty
    forecasts = board.query(kind="forecast", status="current")
    forecast = forecasts[-1].body if forecasts else {}
    used_by: dict[str, list[str]] = {}
    need: dict[str, float] = {}
    for product, parts in look.parts_of.items():
        facts = look.products.get(product, {})
        demand, packed = _num(facts.get("demand_units")), _num(facts.get("packed_units"))
        for part in parts:
            used_by.setdefault(part, []).append(look.name(product))
            if demand is not None:
                need[part] = need.get(part, 0.0) + max(demand - (packed or 0.0), 0.0)
    incoming = _incoming(board)
    rows = []
    for part in sorted(set(usable) | set(held) | set(used_by)):
        runs_out = (forecast.get("runs_out_at") or {}).get(part)
        rows.append({"part_id": part, "name": look.name(part), "usable": usable.get(part, 0.0),
                     "held": held.get(part, 0.0), "incoming": incoming.get(part, 0.0), "batches": len(batches.get(part, ())),
                     "needed": need.get(part), "used_by": used_by.get(part, []), "runs_out_at": runs_out,
                     "value_of_one_more_eur": _num((forecast.get("shadow_price") or {}).get(part))})
    rows.sort(key=lambda r: (r["runs_out_at"] is None, r["runs_out_at"] or "", r["name"]))
    return rows


def _incoming(board: Any) -> dict[str, float]:
    """Units on the way per part: shipments not yet delivered, and what the agents bought."""
    shipments: dict[str, dict[str, Any]] = {}
    for post in board.query(kind="fact", status="active", subject_prefix="shipment/", limit=-1):
        b = post.body
        if b.get("entity") == "shipment" and not b.get("quarantined"):
            shipments.setdefault(str(b.get("key")), {})[b.get("field")] = b.get("value")
    found: dict[str, float] = {}
    for fields in shipments.values():
        parts = fields.get("part_ids") or ()
        qty = _num(fields.get("qty_shipped")) or _num(fields.get("qty"))
        if len(parts) != 1 or qty is None or fields.get("status") in ("delivered", "received", "cancelled"):
            continue
        found[str(parts[0])] = found.get(str(parts[0]), 0.0) + qty
    for action in board.query(kind="action", limit=-1):
        params = action.body.get("params") or {}
        qty = _num(params.get("qty"))
        if action.body.get("kind") in ("accept_offer", "place_order") and action.status in _LIVE and qty is not None:
            found[str(params.get("part_id"))] = found.get(str(params.get("part_id")), 0.0) + qty
    return found


def _purchases(board: Any, look: _Lookup) -> list[dict[str, Any]]:
    listed: dict[tuple[str, str], Any] = {}
    for post in board.query(kind="fact", status="active", subject_prefix="supplier_part/", limit=-1):
        b = post.body
        if b.get("field") == "unit_price" and "|" in str(b.get("key")):
            supplier, part = str(b["key"]).split("|", 1)
            listed[(supplier, part)] = b.get("value")
    rows = []
    for action in sorted(board.query(kind="action", limit=-1), key=lambda p: _number(p.id)):
        body = action.body
        params = body.get("params") or {}
        if body.get("kind") not in ("accept_offer", "place_order") or action.status not in _LIVE or body.get("duplicate_of"):
            continue
        supplier, part = str(params.get("supplier_id")), str(params.get("part_id"))
        rows.append({"post_id": action.id, "at": body.get("executed_at") or action.created_at, "status": action.status,
                     "supplier": look.name(supplier), "part": look.name(part), "qty": _num(params.get("qty")),
                     "unit_price": _num(params.get("unit_price")), "currency": params.get("currency"),
                     "listed_price": _num(listed.get((supplier, part))), "cost_eur": _num(body.get("cost_eur")),
                     "reference": params.get("offer_key"), "arrives_at": params.get("arrives_at")})
    return rows


def _negotiations(board: Any, look: _Lookup) -> list[dict[str, Any]]:
    talks: dict[str, dict[str, Any]] = {}
    for event in board.events_since(0):
        if event["type"] != NEGOTIATION_EVENT:
            continue
        p = event["payload"]
        ids = [str(i) for i in p.get("question_ids") or ()]
        key = next((k for k, t in talks.items() if set(ids) & set(t["question_ids"])), None) or (ids[0] if ids else "")
        talk = talks.setdefault(key, {"id": key, "supplier_id": p.get("to_id"), "supplier": look.name(p.get("to_id")),
                                      "question_ids": ids, "mistakes": [], "terms": [], "stage": None, "steps": []})
        talk["question_ids"] = list(dict.fromkeys([*talk["question_ids"], *ids]))
        if p.get("faults") and not talk["mistakes"]:
            talk["mistakes"] = [{"what": look.plain(f"{f.get('entity')} {f.get('key')}"), "detail": f.get("detail"),
                                 "copies_at_risk": f.get("copies_at_risk"), "defect_id": f.get("defect_id")}
                                for f in p["faults"]]
        stage = str(p.get("stage"))
        if stage == "replied":
            talk["terms"] = p.get("terms") or []
        talk["stage"] = stage
        talk["steps"].append({"at": event["ts"], "stage": stage, "by": event["actor"],
                              "text": _STAGE_TEXT.get(stage, stage), "ask": p.get("ask"),
                              "message_id": p.get("message_id"), "terms": p.get("terms") or []})
    for question in board.query(kind="question", limit=-1):
        if not str(question.body.get("topic", "")).startswith("supplier:price-reply:"):
            continue
        for talk in talks.values():
            if set(talk["question_ids"]) & set(question.depends_on):
                waiting = question.status in ("open", "asked")
                talk["decision"] = {"post_id": question.id, "role": question.body.get("to_id"),
                                    "status": "waiting" if waiting else question.status}
    rows = list(talks.values())
    for talk in rows:
        talk["opened_at"] = talk["steps"][0]["at"]
        talk["updated_at"] = talk["steps"][-1]["at"]
    rows.sort(key=lambda t: t["updated_at"], reverse=True)
    return rows


def supply_view(board: Any) -> dict[str, Any]:
    look = _Lookup(board)
    purchases = _purchases(board, look)
    committed = [p for p in purchases if p["status"] == "executed"]
    talks = _negotiations(board, look)
    return {"stock": _stock(board, look), "purchases": purchases, "negotiations": talks,
            "totals": {"spend_eur": sum(p["cost_eur"] or 0.0 for p in committed),
                       "committed_orders": len(committed),
                       "negotiations_open": sum(1 for t in talks if t["stage"] in ("asked", "reminded")),
                       "negotiations_replied": sum(1 for t in talks if t["stage"] == "replied"),
                       "negotiations_unanswered": sum(1 for t in talks if t["stage"] == "no_answer")}}

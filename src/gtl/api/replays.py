"""Recorded Gym scenarios for the Environment screen: what happened, what the agents did, what it paid.

Run `python -m gtl.api.replays` to rewrite web/environment-replays.json. Each record lists the problems of one
scenario with the data an agent could see, the actions the agents scheduled for each one (or why none was), and
the episode reward with and without agents.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from gtl.config import load_tenant
from gtl.gym.diagnose import diagnose
from gtl.gym.runner import play_episode
from gtl.gym.scenario import Scenario, generate, soony_real
from gtl.gym.world import WorldState

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BASE = ROOT / "tests" / "fixtures" / "soony_base.json"
DEFAULT_OUT = ROOT / "web" / "environment-replays.json"
# (tier, split, seeds) of the recorded scenarios, after the launch replay of the real situation.
DEFAULT_SET = (("base", "train", range(1, 3)), ("hard", "train", range(1, 5)), ("hard", "val", range(1, 17)))

WHY_NOT = {
    "too_late": "The agents acted, but the remedy landed after the deadline.",
    "not_approved": "The agents proposed a remedy, but it was not approved.",
    "floor_ignored": "The agents knew, but no action reached the warehouse floor.",
    "not_chosen": "The agents priced a remedy but did not request it.",
    "not_worth_it": "The agents priced the remedy and found it worth nothing before the deadline.",
    "no_candidate": "The agents saw the problem but had no remedy to offer.",
    "not_visible": "The evidence only comes on request, and nobody asked.",
    "no_conflict": "The evidence reached the Board, but nobody flagged the conflict.",
}


def _hour(world: WorldState, stamp: datetime | str) -> float:
    at = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
    return round((at - world.start).total_seconds() / 3600.0, 1)


def _pcs(value: float | None) -> str:
    return "unknown" if value is None else f"{value:,.0f} pcs"


def _supplier(world: WorldState, supplier_id: str | None) -> str:
    sup = world.suppliers.get(supplier_id or "")
    return f"{sup.name} ({supplier_id})" if sup and sup.name else str(supplier_id)


def _shipment(world: WorldState, shipment_id: str) -> list[list[str]]:
    s = next((s for s in world.shipments if s.shipment_id == shipment_id), None)
    if s is None:
        return [["Shipment", shipment_id]]
    rows = [["Shipment", shipment_id], ["Supplier", _supplier(world, s.supplier_id)], ["Part", s.part_id]]
    return rows + ([["Route", f"{s.origin} to {s.destination}"]] if s.origin and s.destination else [])


def facts(d: Any, world: WorldState) -> list[list[str]]:
    """The data behind one disruption, as label and value pairs, read from the world the agents start in."""
    p = d.to_dict()
    kind = p["type"]
    batch = world.find_batch(p["part_id"], p["batch"]) if "batch" in p else None
    if kind == "booking_error":
        rows = [["Part", p["part_id"]], ["Batch", p["batch"]],
                ["ERP stock", _pcs(batch.book_qty if batch else None)], ["Real stock", _pcs(p["true_qty"])]]
        if p.get("supplier_id") and p.get("offer_qty"):
            rows.append(["Spare offer", f"{_pcs(p['offer_qty'])} from {_supplier(world, p['supplier_id'])}"])
        return rows
    if kind == "rival_offers":
        return [["Part", p["part_id"]], ["Batch", p["batch"]],
                ["ERP stock", _pcs(batch.book_qty if batch else None)], ["Real stock", _pcs(p["true_qty"])],
                ["Cheap offer", f"{_supplier(world, p['cheap_supplier'])}, {p['cheap_price']:g} {p['currency']}/pc, "
                                f"{p['cheap_hours']:g} h"],
                ["Reliable offer", f"{_supplier(world, p['safe_supplier'])}, {p['safe_price']:g} {p['currency']}/pc, "
                                   f"{p['safe_hours']:g} h"]]
    if kind == "short_shipment":
        s = next((s for s in world.shipments if s.shipment_id == p["shipment_id"]), None)
        return _shipment(world, p["shipment_id"]) + [["Booked", _pcs(s.book_qty if s else None)],
                                                     ["Actually shipped", _pcs(p["shipped_qty"])]]
    if kind == "failed_lot":
        return [["Part", p["part_id"]], ["Batch", p["batch"]], ["Quantity", _pcs(batch.book_qty if batch else None)],
                ["ERP status", "usable"], ["Inspection", f"failed, {p['defect_rate']:.0%} defective"]]
    if kind == "stuck_at_port":
        s = next((s for s in world.shipments if s.shipment_id == p["shipment_id"]), None)
        return _shipment(world, p["shipment_id"]) + [["Quantity", _pcs(s.qty if s else None)],
                                                     ["Tracker", "in transit"],
                                                     ["Reality", "at the port, needs pick-up and a dock slot"]]
    if kind == "terms_hold":
        return _shipment(world, p["shipment_id"]) + [["Purchase order", p["po_number"]],
                                                     ["Terms on the PO", p["po_term"]],
                                                     ["Terms in the contract", p["contract_term"]]]
    if kind == "decoy":
        return [["Part", p["part_id"]], ["Plan flag", "CHECK, cover shows negative"],
                ["Reality", "fitted by the supplier, nothing to do" if p["kind"] == "fitted_part"
                 else "stock is there, nothing to do"]]
    return [[k.replace("_", " ").capitalize(), str(v)] for k, v in p.items() if k != "type"]


def record(scenario: Scenario, tenant: dict, record_id: str | None = None) -> dict[str, Any]:
    """One scenario played by the registered agents, as the Environment screen replays it."""
    start = scenario.build(tenant)
    result, board, world = play_episode(scenario, tenant)
    misses = {m["problem"]: m for m in diagnose(result, scenario, board) if m.get("problem")}
    posts = {p.body.get("idempotency_key"): p for p in board.query(kind="action", limit=1_000_000)}
    scheduled = [e for e in world.ledger if e["kind"] == "action" and e.get("status") == "scheduled"
                 and e.get("remedy_id")]
    events = []
    for d in scenario.disruptions:
        label = d.labels()
        seen = [at for at, _ in d.observables(start)]
        responses = []
        for entry in scheduled:
            if not entry["remedy_id"].startswith(label["id"] + ":"):
                continue
            post = posts.get(entry.get("idempotency_key"))
            if post is None:
                continue
            responses.append({"action": post.body["kind"], "agent": post.author,
                              "hour": _hour(start, post.created_at),
                              "cost_eur": float(post.body.get("cost_eur") or 0.0)})
        miss = misses.get(label["id"])
        why = None
        if label["decoy"]:
            why = "Nothing was wrong. Holding back was the right call."
        elif not responses:
            why = WHY_NOT.get(miss["cause"], "No remedy was recorded.") if miss else "No remedy was needed in time."
        events.append({"type": label["type"], "problem": label["problem"], "facts": facts(d, start),
                       "hour": _hour(start, min(seen)) if seen else 0.0,
                       "found": label["id"] in result.problems_found, "decoy": label["decoy"],
                       "responses": sorted(responses, key=lambda r: r["hour"]), "why_not": why})
    packed = result.breakdown.get("packed", {})
    return {"id": record_id or scenario.id, "events": events, "packed": float(sum(packed.values())),
            "hours": _hour(start, start.deadline),
            "reward": result.reward, "idle": result.do_nothing, "best": result.oracle}


def record_set(base: dict, tenant: dict) -> list[dict[str, Any]]:
    out = [record(soony_real(base), tenant, "launch-replay")] if "real_situation" in base else []
    for tier, split, seeds in DEFAULT_SET:
        out += [record(generate(base, seed, split, tenant=tenant, tier=tier), tenant) for seed in seeds]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=str(DEFAULT_BASE))
    parser.add_argument("--tenant", default="soony")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    base = json.loads(Path(args.base).read_text(encoding="utf-8"))
    tenant = load_tenant(args.tenant, root=str(ROOT / "tenants"))
    records = record_set(base, tenant)
    Path(args.out).write_text("[\n" + ",\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n]\n",
                              encoding="utf-8")
    print(f"{len(records)} scenarios written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

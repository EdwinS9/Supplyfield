"""Supplier search for a material shortage, using the Board as its correspondence history."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from gtl.config import supplier_directory
from gtl.plan.inputs import _as_list, _num, _table

PREFIXES = ("spare:", "recovery:", "discover:")


def sourcing_asks(board: Any, tenant: dict, gaps: dict[str, float], horizon: datetime | None) -> list[dict]:
    if not gaps:
        return []
    catalog = _table(board, "supplier_part")
    vendors = _table(board, "supplier")
    materials = _table(board, "part")
    prospects = supplier_directory(tenant)
    listed: dict[str, set[str]] = defaultdict(set)
    offers: dict[str, set[str]] = defaultdict(set)
    for key in catalog:
        vendor, _, part = key.partition("|")
        listed[part].add(vendor)
    for item in _table(board, "po_line").values():
        if item.get("part_id") and item.get("supplier_id"):
            listed[item["part_id"]].add(item["supplier_id"])
    for shipment in _table(board, "shipment").values():
        if shipment.get("supplier_id"):
            for part in _as_list(shipment.get("part_ids")):
                listed[part].add(shipment["supplier_id"])
    for key, item in _table(board, "offer").items():
        offers[item.get("part_id")].add(key.partition("|")[0])
    # A later demand deadline starts a fresh search even when the supplier and part are unchanged.
    window = f":by:{horizon.isoformat()}" if horizon else ""
    history = {}
    for q in board.query(kind="question", limit=-1):
        topic = q.body.get("topic") or ""
        if not topic.startswith(PREFIXES):
            continue
        history[q.body.get("to_id"), topic] = q.status
        # Questions created before deadline-scoped topics still own their open conversation.
        if window and ":by:" not in topic and (q.status in ("open", "asked") or
                (q.created_at and q.created_at[:10] == horizon.date().isoformat())):
            history[q.body.get("to_id"), topic + window] = q.status
    site = tenant.get("site") or "our receiving site"
    out = []
    for part, qty in sorted(gaps.items()):
        need = f"up to {qty:g} units of {part}"
        if horizon:
            need += f" by {horizon.isoformat()}"
        category = materials.get(part, {}).get("category")
        candidates = listed[part] | offers[part]
        ordered = sorted(candidates, key=lambda v: (-(_num(catalog.get(f"{v}|{part}", {}).get("share")) or 0), v))
        # A directory entry is a lead for an RFQ. It does not grant purchasing approval.
        ordered += sorted(v for v, entry in prospects.items() if v not in candidates and
                          (part in entry.get("parts", []) or category and category in entry.get("categories", [])))
        # Same-category suppliers can source a new part or introduce a distributor.
        if category:
            related = {v for other, vendors_for_part in listed.items()
                       if materials.get(other, {}).get("category") == category for v in vendors_for_part}
            ordered.extend(sorted(related - set(ordered)))
        direct = f"spare:{part}{window}"
        recovery = f"recovery:{part}{window}"
        for vendor in ordered:
            if vendors.get(vendor, {}).get("release_status") == "blocked":
                continue
            if not (vendors.get(vendor, {}).get("contact_email") or prospects.get(vendor, {}).get("contact_email")):
                continue
            first = history.get((vendor, direct))
            second = history.get((vendor, recovery))
            if first in ("open", "asked") or second in ("open", "asked"):
                break
            if first == "expired" or second in ("answered", "expired"):
                continue
            if first == "answered" or vendor in offers[part]:
                topic = recovery
                text = (f"For {need}, can you split a lot, divert regional stock or expedite a partial delivery "
                        f"to {site}? Quote the earliest and cheapest feasible options separately, with quantity, "
                        "confirmed arrival, total delivered price and expiry. If neither works, identify an "
                        "authorized alternate source with a verifiable contact.")
            else:
                topic = direct
                text = (f"Can you supply {need} from finished stock to {site}? Please quote available quantity, "
                        "earliest confirmed arrival, and best all-in delivered price. If regular delivery misses "
                        "the deadline, include any partial stock available locally. If unavailable, please "
                        "identify a manufacturer or distributor with a verified contact for this item.")
            out.append(dict(to_kind="supplier", to_id=vendor, topic=topic, text=text, part=part))
            break
        else:
            topic = f"discover:{part}{window}"
            if ("buyer", topic) not in history:
                out.append(dict(to_kind="role", to_id="buyer", topic=topic, part=part,
                                text=f"Find a new source for {need} at {site}. Check manufacturer referrals and "
                                     "authorized distributors. Record the supplier, verified contact and parts "
                                     "supplied so the liaison can request comparable delivered quotes."))
    return out

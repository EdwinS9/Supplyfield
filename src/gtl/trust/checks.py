"""Cross-entity rule checks over current facts.

Each check is `check(board, now) -> list[Conflict]`. None names a specific
part, supplier or number; they read only vocabulary from `contracts.ENTITIES`.
Every conflict lists the disputed side first, so the steward can find the
record that is probably wrong.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from gtl.contracts import Board, Conflict, Fact
from gtl.trust.common import fact_table, parse_ts, to_number, values_match

Table = dict[str, dict[str, dict[str, Fact]]]
RECEIPT_OVER_ORDER = 1.10
STALE_FEED_HOURS = 12
RELEASE_MAX_DAYS = 90
# Canonical strings from contracts.VALUES.
STALE_ARRIVAL_DAYS = 14
# po_line.status values that mean the line is finished (contracts.VALUES knows delivered and cancelled).
DONE_PO_STATUSES = ("closed", "delivered", "received", "completed", "cancelled")
STALE_STATUSES = ("in_transit", "in_delivery", "booked")


def _ids(*facts: Fact | None) -> tuple[str, ...]:
    out: list[str] = []
    for f in facts:
        for cid in (f.claim_ids if f else ()):
            if cid not in out:
                out.append(cid)
    return tuple(out)


def _first(f: Fact | None) -> str | None:
    return f.claim_ids[0] if f and f.claim_ids else None


def _low(f: Fact | None) -> str:
    return str(f.value).strip().casefold() if f else ""


def receipt_vs_order(board: Board, now: datetime) -> list[Conflict]:
    out = []
    for key, f in fact_table(board).get("receipt", {}).items():
        rec, ordered = f.get("qty_received"), f.get("qty_ordered")
        a, b = to_number(rec.value if rec else None), to_number(ordered.value if ordered else None)
        if rec is None or ordered is None or a is None or b is None or a <= RECEIPT_OVER_ORDER * b:
            continue
        out.append(Conflict("receipt", key, "qty_received", _ids(rec, ordered), "receipt_vs_order",
                            f"Received {a:g} but only {b:g} were ordered.", _first(ordered)))
    return out


def _note_line(t: Table, note: str, part: str) -> dict[str, Fact] | None:
    """The delivery note line for a part: the line naming that part, else the only line, else None."""
    lines = {k: f for k, f in t.get("delivery_note", {}).items()
             if (k == note or k.startswith(f"{note}|")) and "qty" in f}
    for f in lines.values():
        if part and f.get("part_id") is not None and str(f["part_id"].value) == part:
            return f
    return next(iter(lines.values())) if len(lines) == 1 else None


def receipt_vs_note(board: Board, now: datetime) -> list[Conflict]:
    """Receipts of one part against one note are compared as a sum with that note's line."""
    t, out = fact_table(board), []
    groups: dict[tuple[str, str], list[tuple[str, Fact]]] = {}
    for key, f in t.get("receipt", {}).items():
        rec, note_key = f.get("qty_received"), f.get("delivery_note")
        if rec is None or note_key is None or to_number(rec.value) is None:
            continue
        part = f.get("part_id")
        groups.setdefault((str(note_key.value), str(part.value) if part else ""), []).append((key, rec))
    for (note_key, part), recs in groups.items():
        line = _note_line(t, note_key, part)
        total = sum(to_number(r.value) or 0.0 for _, r in recs)
        if line is None or values_match(total, line["qty"].value):
            continue
        note = line["qty"]
        if len(recs) == 1:
            key, rec = recs[0]
            out.append(Conflict("receipt", key, "qty_received", _ids(rec, note), "receipt_vs_note",
                                f"Receipt books {rec.value} but delivery note {note_key} says {note.value}.",
                                _first(note)))
            continue
        biggest = max(recs, key=lambda kr: to_number(kr[1].value) or 0.0)[0]
        names = ", ".join(f"{k} ({r.value})" for k, r in recs)
        out.append(Conflict("receipt", biggest, "qty_received", _ids(*(r for _, r in recs), note),
                            "receipt_vs_note",
                            f"Receipts {names} sum to {total:g} but delivery note {note_key} says {note.value}.",
                            _first(note), tuple(k for k, _ in recs)))
    return out


def inspection_vs_stock(board: Board, now: datetime) -> list[Conflict]:
    t, out = fact_table(board), []
    for key, f in t.get("inspection", {}).items():
        result = f.get("result")
        if _low(result) != "failed" or _low(f.get("concession")) == "true":
            continue
        for skey, sf in t.get("stock", {}).items():
            if skey.rsplit("|", 1)[-1] != key:
                continue
            status = sf.get("quality_status")
            if status is not None and _low(status) != "unrestricted":
                continue
            out.append(Conflict("stock", skey, "quality_status", _ids(status or sf.get("qty"), result),
                                "inspection_vs_stock",
                                f"Inspection {key} failed but the batch is {status.value if status else 'unmarked'}.",
                                _first(result)))
    return out


def revision_match(board: Board, now: datetime) -> list[Conflict]:
    """Confirmations naming a revision other than the one the part needs, or two revisions for one part.

    One conflict per part and confirmed revision, on the first confirmation, listing the rest in `group`.
    """
    t, out = fact_table(board), []
    confs = {k: f["revision"] for k, f in t.get("order_confirmation", {}).items() if "revision" in f}
    by_part: dict[str, dict[str, list[str]]] = {}  # part -> normalised revision -> confirmation keys
    for key in sorted(confs):
        parts = key.split("|")
        if len(parts) >= 2:
            by_part.setdefault(parts[1], {}).setdefault(_low(confs[key]), []).append(key)
    for part, revs in by_part.items():
        req = t.get("part", {}).get(part, {}).get("required_revision")
        firsts = [keys[0] for keys in revs.values()]
        for keys in revs.values():
            key = keys[0]
            if req and not values_match(req.value, confs[key].value):
                out.append(Conflict("order_confirmation", key, "revision", _ids(confs[key], req),
                                    "revision_match",
                                    f"Confirmation says revision {confs[key].value} but the part needs {req.value}"
                                    + (f" ({len(keys)} confirmations)." if len(keys) > 1 else "."),
                                    _first(req), tuple(keys) if len(keys) > 1 else ()))
        for i, k1 in enumerate(firsts):
            for k2 in firsts[i + 1:]:
                out.append(Conflict("order_confirmation", f"{k1}~{k2}", "revision",
                                    _ids(confs[k1], confs[k2]), "revision_match",
                                    f"Two confirmations for one part differ: "
                                    f"{confs[k1].value} and {confs[k2].value}."))
    return out


def _release_context(t: Table, skey: str) -> tuple[dict[str, Fact], Fact | None] | None:
    """A batch's own facts and its part's required revision; None when the part needs none."""
    part = t.get("part", {}).get(skey.split("|")[1] if "|" in skey else "", {})
    req = part.get("required_revision")
    return None if req is None else (part, req)


def revision_not_released(board: Board, now: datetime) -> list[Conflict]:
    """A batch printed to a revision other than the required one, where the old one may not be used up."""
    t, out = fact_table(board), []
    for skey, f in t.get("stock", {}).items():
        rev, ctx = f.get("revision"), _release_context(t, skey)
        if rev is None or ctx is None:
            continue
        part, req = ctx
        if values_match(rev.value, req.value) or _low(part.get("use_up_old_revision")) == "true":
            continue
        out.append(Conflict("stock", skey, "revision", _ids(rev, req), "revision_not_released",
                            f"Batch {skey.rsplit('|', 1)[-1]} is revision {rev.value} but the part needs "
                            f"{req.value} and the old revision may not be used up.", _first(req)))
    return out


def revision_unknown(board: Board, now: datetime) -> list[Conflict]:
    """A received batch of a part with a required revision whose paperwork names none."""
    t, out = fact_table(board), []
    for skey, f in t.get("stock", {}).items():
        qty, ctx = f.get("qty"), _release_context(t, skey)
        batch = skey.rsplit("|", 1)[-1]
        if qty is None or "revision" in f or ctx is None or batch not in t.get("receipt", {}):
            continue
        if _low(ctx[0].get("use_up_old_revision")) == "true":
            continue  # the old revision may be used up, so a missing revision cannot hurt
        out.append(Conflict("stock", skey, "revision", _ids(qty, ctx[1]), "revision_unknown",
                            f"No document says which revision batch {batch} carries; the part needs "
                            f"{ctx[1].value}."))
    return out


def terms_match(board: Board, now: datetime) -> list[Conflict]:
    """Open order lines whose incoterm differs from the contract: one conflict per supplier and pair of terms.

    The conflict sits on the open line with the latest promised date and lists every line it covers.
    """
    t, out = fact_table(board), []
    groups: dict[tuple[str, str, str], list[tuple[str, dict[str, Fact], Fact]]] = {}
    for key, f in sorted(t.get("po_line", {}).items()):
        inc, sup = f.get("incoterm"), f.get("supplier_id")
        if inc is None or sup is None or _low(f.get("status")) in DONE_PO_STATUSES:
            continue
        term = t.get("contract_term", {}).get(f"{sup.value}|incoterm", {}).get("value")
        if term is None or values_match(inc.value, term.value):
            continue
        groups.setdefault((str(sup.value), _low(inc), _low(term)), []).append((key, f, term))
    for lines in groups.values():
        def latest(item: tuple[str, dict[str, Fact], Fact]) -> tuple[float, str]:
            when = parse_ts(item[1]["promised_date"].value) if "promised_date" in item[1] else None
            return (when.timestamp() if when else 0.0, item[0])
        key, f, term = max(lines, key=latest)
        inc = f["incoterm"]
        n = len(lines)
        detail = (f"Order line says {inc.value} but the contract says {term.value}." if n == 1 else
                  f"{n} open order lines say {inc.value} but the contract says {term.value}.")
        out.append(Conflict("po_line", key, "incoterm", _ids(inc, term), "terms_match", detail,
                            group=tuple(k for k, _, _ in lines) if n > 1 else ()))
    return out


def shipment_vs_advice(board: Board, now: datetime) -> list[Conflict]:
    out = []
    for key, f in fact_table(board).get("shipment", {}).items():
        qty, adv = f.get("qty"), f.get("qty_shipped")
        if qty is None or adv is None or values_match(qty.value, adv.value):
            continue
        out.append(Conflict("shipment", key, "qty", _ids(qty, adv), "shipment_vs_advice",
                            f"Shipment lists {qty.value} but the supplier advice says {adv.value}.", _first(adv)))
    return out


def _earliest_deadline(t: Table) -> datetime | None:
    dates = [d for f in t.get("product", {}).values() if "deadline" in f
             for d in [parse_ts(f["deadline"].value)] if d is not None]
    return min(dates) if dates else None


def stale_feed(board: Board, now: datetime) -> list[Conflict]:
    """Shipments in transit with an old status that can still matter: one conflict per supplier.

    Still matters: expected arrival (`eta_site`, else `eta`) at most 14 days past and, when a product
    deadline is known, not later than the earliest deadline plus 14 days. The conflict sits on the
    shipment arriving first and lists the others in `group`.
    """
    t = fact_table(board)
    deadline = _earliest_deadline(t)
    by_owner: dict[str, list[tuple[datetime | None, str, dict[str, Fact]]]] = {}
    for key, f in sorted(t.get("shipment", {}).items()):
        status, upd = f.get("status"), f.get("status_updated_at")
        if status is None or upd is None or _low(status) not in STALE_STATUSES:
            continue
        when = parse_ts(upd.value)
        if when is None or now - when <= timedelta(hours=STALE_FEED_HOURS):
            continue
        expected = f.get("eta_site") or f.get("eta")
        arr = parse_ts(expected.value) if expected else None
        if arr is not None and (arr < now - timedelta(days=STALE_ARRIVAL_DAYS)
                                or (deadline is not None and arr > deadline + timedelta(days=STALE_ARRIVAL_DAYS))):
            continue
        owner = f.get("supplier_id") or f.get("carrier_id")
        by_owner.setdefault(str(owner.value) if owner else key, []).append((arr, key, f))
    out = []
    for rows in by_owner.values():
        arr, key, f = min(rows, key=lambda r: (r[0] is None, r[0].timestamp() if r[0] else 0.0, r[1]))
        upd = f["status_updated_at"]
        n = len(rows)
        detail = (f"Shipment is in transit but its status was last updated {upd.value}." if n == 1 else
                  f"{n} shipments are in transit with stale status; the earliest to arrive was last updated {upd.value}.")
        out.append(Conflict("shipment", key, "status_updated_at", _ids(f["status"], upd), "stale_feed", detail,
                            group=tuple(k for _, k, _ in rows) if n > 1 else ()))
    return out


def release_valid(board: Board, now: datetime) -> list[Conflict]:
    out = []
    for key, f in fact_table(board).get("supplier", {}).items():
        st, dt = f.get("release_status"), f.get("release_date")
        if st is None or dt is None or _low(st) != "conditional":
            continue
        when = parse_ts(dt.value)
        if when is None or now - when <= timedelta(days=RELEASE_MAX_DAYS):
            continue
        out.append(Conflict("supplier", key, "release_date", _ids(st, dt), "release_valid",
                            f"Release is conditional and dates from {dt.value}, older than {RELEASE_MAX_DAYS} days."))
    return out


def bank_change(board: Board, now: datetime) -> list[Conflict]:
    out = []
    for key, f in fact_table(board).get("notice", {}).items():
        kind = f.get("kind")
        if kind is not None and _low(kind) == "bank_change":
            out.append(Conflict("notice", key, "kind", _ids(kind, f.get("detail")), "bank_change",
                                f"Notice {key} announces a bank detail change ({kind.value})."))
    return out


def date_order(board: Board, now: datetime) -> list[Conflict]:
    out = []
    for key, f in fact_table(board).get("shipment", {}).items():
        etd, eta = f.get("etd"), f.get("eta")
        if etd is None or eta is None:
            continue
        a, b = parse_ts(etd.value), parse_ts(eta.value)
        if a is not None and b is not None and a > b:
            out.append(Conflict("shipment", key, "eta", _ids(etd, eta), "date_order",
                                f"Departure {etd.value} is after arrival {eta.value}."))
    return out


CHECKS: list[Callable[[Board, datetime], list[Conflict]]] = [
    receipt_vs_order, receipt_vs_note, inspection_vs_stock, revision_match, revision_not_released,
    revision_unknown, terms_match,
    shipment_vs_advice, stale_feed, release_valid, bank_change, date_order,
]

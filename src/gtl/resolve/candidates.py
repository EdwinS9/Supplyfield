"""Candidate fixes for supply shortfalls, built only from facts and posts on the Board.

Pure functions. Nothing here names a part, supplier or number: every value
comes from the Board, the forecast or the tenant config.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from gtl import money
from gtl.contracts import Action, Arrival, Board, Forecast, PlanInput
from gtl.plan.inputs import _as_list, _as_of_end, _num, _parse_dt, _table, duplicate_shipments

AGENT = "resolver"
_DONE = ("delivered", "cancelled")
_FREE_STATUSES = ("failed", "rejected", "blocked", "stale")  # an action in these may be asked again
_MAX_FAILURES = 3  # a request that failed this often is not repeated; a person has to look
_NOT_TERMS = ("on_time_rate",)  # contract_term facts that describe the supplier, not delivery terms
_SHORT = 0.5  # units; below this a shortfall counts as zero


@dataclass(frozen=True)
class Effect:
    """How a candidate changes a PlanInput."""

    add_stock: dict[str, float] = field(default_factory=dict)
    add_arrivals: tuple[Arrival, ...] = ()
    priority: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Candidate:
    id: str  # stable: the idempotency key of the first action
    title: str
    actions: tuple[Action, ...]
    effect: Effect
    cost_eur: float | None  # None: unpriced, a person must price it
    needs: str = ""  # free text: what a person must provide
    parts: tuple[str, ...] = ()  # parts it fixes, used for deadlines
    unpriced_reason: str = ""  # why cost_eur is None
    always: bool = False  # costs nothing and is what makes the floor follow the plan: request it even at zero gain
    context: dict[str, Any] = field(default_factory=dict)  # facts for the model about the offer, not used by pricing


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def forecast_from_body(body: dict[str, Any]) -> Forecast:
    data = dict(body)
    data["per_hour"] = {k: tuple(v) for k, v in data.get("per_hour", {}).items()}
    data["fact_ids"] = tuple(data.get("fact_ids", ()))
    return Forecast(**data)


def hour_index(hours: tuple[str, ...], when: datetime, tz: ZoneInfo) -> int:
    """First plan hour at or after `when`; len(hours) when none (the planner ignores it)."""
    for i, h in enumerate(hours):
        t = _parse_dt(h, tz)
        if t is not None and t >= when:
            return i
    return len(hours)


def _taken_keys(board: Board) -> set[str]:
    """Keys of actions not to request again: live ones, and ones that already failed too often."""
    failed: dict[str, int] = {}
    taken: set[str] = set()
    for p in board.query(kind="action", limit=100_000):
        key = str(p.body.get("idempotency_key"))
        if p.status not in _FREE_STATUSES:
            taken.add(key)
        elif p.status == "failed":
            failed[key] = failed.get(key, 0) + 1
    return taken | {k for k, n in failed.items() if n >= _MAX_FAILURES}


def _category(parts: dict[str, dict[str, Any]], part_id: str) -> Any:
    return parts.get(part_id, {}).get("category")


def ship_part_qtys(f: dict[str, Any], po_lines: dict[str, dict[str, Any]]) -> dict[str, float]:
    """Quantity per part in a shipment, by the planner's rules."""
    pos = _as_list(f.get("po_numbers"))
    parts = _as_list(f.get("part_ids"))
    if not parts:
        parts = sorted({str(v.get("part_id")) for k, v in po_lines.items()
                        if k.partition("|")[0] in pos and v.get("part_id")})
    direct = _num(f.get("qty_shipped"))
    if direct is None:
        direct = _num(f.get("qty"))
    out: dict[str, float] = {}
    for part in parts:
        if len(parts) == 1 and direct is not None:
            qty: float | None = direct
        else:
            total, found, seen = 0.0, False, set()
            for key, v in po_lines.items():
                q = _num(v.get("qty"))
                po, _, item = key.partition("|")
                # Two ERP exports number the same order item as 2 and as 20 (SAP steps of ten): count it once.
                number = int(item) if item.isdigit() else None
                item_key = (po, item if number is None else number * 10 if number < 10 else number)
                if po in pos and v.get("part_id") == part and v.get("status") not in _DONE and q is not None \
                        and item_key not in seen:
                    seen.add(item_key)
                    total += q
                    found = True
            qty = total if found else None
        if qty is not None and qty > 0:
            out[part] = qty
    return out


def transit_days(board: Board, origin: str, site: str) -> float:
    for r in _table(board, "route").values():
        if r.get("origin") == origin and r.get("destination") == site:
            days = _num(r.get("transit_days"))
            if days is not None:
                return days
    return 1.0


def _quote_text(m: money.Money) -> str:
    quote = f"{m.currency or '?'} {m.amount:,.0f}"
    if m.currency == "EUR":
        return quote
    if m.eur is None:
        return f"{quote} (not priced: {m.unpriced_reason})"
    return f"{quote} (EUR {m.eur:,.2f}, rate of {m.fx_date})"


# ---------------------------------------------------------------------------
# generators
# ---------------------------------------------------------------------------
def _offer_context(board: Board, fc: Forecast, pi: PlanInput, tz: ZoneInfo, supplier: str, part: str,
                   arrives: datetime) -> dict[str, Any]:
    """What the model may weigh about an offer: who, the promised arrival, the slack to the run-out, the record."""
    out_at = _parse_dt(fc.runs_out_at.get(part) or (pi.hours[-1] if pi.hours else None), tz)
    context: dict[str, Any] = {"supplier_id": supplier, "part_id": part, "promised_arrival": arrives.isoformat()}
    if out_at is not None:
        context["margin_hours"] = round((out_at - arrives).total_seconds() / 3600.0, 1)
    rate = _num(_table(board, "contract_term").get(f"{supplier}|on_time_rate", {}).get("value"))
    if rate is not None:
        context["supplier_on_time_rate"] = rate
    return context


def _offers(board: Board, tenant: dict, now: datetime, fc: Forecast, pi: PlanInput,
            tz: ZoneInfo) -> list[Candidate]:
    relevant = {p for p, v in fc.shadow_price.items() if v > 0}
    relevant |= {p for p in fc.limiting_part.values() if p}
    parts = _table(board, "part")
    contracts = _table(board, "supplier_part")
    out: list[Candidate] = []
    for key, f in sorted(_table(board, "offer").items()):
        part = str(f.get("part_id") or "")
        offer_qty = _num(f.get("qty"))
        if not part or part not in relevant or not offer_qty or offer_qty <= 0:
            continue
        until = _as_of_end(f.get("valid_until"), tz)  # "valid until 22 Nov" holds through that day
        if f.get("valid_until") and (until is None or until <= now):
            continue
        # Cap at what the plan could still use; the planner prices the rest.
        needed = sum(u * pi.bom.get(p, {}).get(part, 0.0) for p, u in pi.products.items())
        qty = min(offer_qty, needed) if needed > 0 else offer_qty
        supplier = key.partition("|")[0]
        price, currency_raw, price_source = _num(f.get("unit_price")), f.get("currency"), "offer"
        if price is None:  # an offer without its own price is at the contract price, when there is one
            contract = contracts.get(f"{supplier}|{part}", {})
            price, currency_raw, price_source = _num(contract.get("unit_price")), contract.get("currency"), "contract"
        surcharge = _num(f.get("surcharge_pct")) or 0.0
        if price is None:
            m = money.Money(Decimal(0), money.normalize_currency(currency_raw), None,
                            unpriced_reason="no price in the offer and no contract price")
            price = 0.0
        else:
            m = money.to_eur(money.line_total(price, qty, surcharge), currency_raw, now, tenant)
        currency = m.currency
        cost = float(m.eur) if m.eur is not None else None
        ready = _num(f.get("ready_hours")) or 0.0
        transit = _num(f.get("transit_hours"))
        known_arrival = transit is not None  # no transit time on record: the arrival is unknown, not immediate
        arrives = now + timedelta(hours=ready + (transit or 0.0))
        context = _offer_context(board, fc, pi, tz, supplier, part, arrives)
        akey = f"accept_offer:{key}"
        params = {
            "supplier_id": supplier, "offer_key": key, "part_id": part, "qty": qty,
            "unit_price": price, "currency": currency, "surcharge_pct": surcharge, "price_source": price_source,
            "amount": float(m.amount), "fx_rate": None if m.fx_rate is None else float(m.fx_rate),
            "fx_date": m.fx_date, "fx_source": m.fx_source, "cost_eur": cost,
            "unpriced_reason": m.unpriced_reason,
            "ready_hours": ready, "transit_hours": transit, "arrives_at": arrives.isoformat(),
            "category": _category(parts, part),
        }
        action = Action("accept_offer", params, AGENT, "", akey, cost_eur=cost or 0.0)
        # With [autonomy] price_offers_by_on_time_record, an offer is worth what it delivers on time:
        # the supplier's on-time record is the chance that the promised arrival holds (the Gym found
        # trusted promises to be the costliest miss of the agents on the hard tier).
        on_time = context.get("supplier_on_time_rate")
        by_record = bool(tenant.get("autonomy", {}).get("price_offers_by_on_time_record"))
        chance = min(1.0, max(0.0, float(on_time))) if by_record and isinstance(on_time, (int, float)) else 1.0
        arrival = Arrival(part, qty, hour_index(pi.hours, arrives, tz), chance, key)
        out.append(Candidate(
            id=akey, title=f"Accept offer from {supplier}: {qty:g} x {part} for {_quote_text(m)}",
            actions=(action,), effect=Effect(add_arrivals=(arrival,) if known_arrival else ()), cost_eur=cost,
            needs=("dock slot at the site for the delivery" if known_arrival else
                   "the supplier must state the transit time before this offer can be priced")
            if cost is not None else f"a person must price this offer: {m.unpriced_reason}",
            parts=(part,), unpriced_reason=m.unpriced_reason or "", context=context))
    return out


def _pickup_arrivals(board: Board, site: str, now: datetime, pi: PlanInput, tz: ZoneInfo,
                     f: dict[str, Any], sid: str, po_lines: dict[str, dict[str, Any]]
                     ) -> tuple[datetime, dict[str, float], tuple[Arrival, ...]]:
    arrives = now + timedelta(days=transit_days(board, str(f.get("destination") or ""), site))
    idx = hour_index(pi.hours, arrives, tz)
    qtys = ship_part_qtys(f, po_lines)
    return arrives, qtys, tuple(Arrival(p, q, idx, 1.0, sid) for p, q in sorted(qtys.items()))


def _pickups(board: Board, tenant: dict, now: datetime, fc: Forecast, pi: PlanInput,
             tz: ZoneInfo) -> list[Candidate]:
    site = str(tenant.get("site", ""))
    po_lines = _table(board, "po_line")
    out: list[Candidate] = []
    ships = _table(board, "shipment")
    duplicates = duplicate_shipments(ships)
    for sid, f in sorted(ships.items()):
        if f.get("status") != "arrived_port" or sid in duplicates:
            continue
        arrives, qtys, arrivals = _pickup_arrivals(board, site, now, pi, tz, f, sid, po_lines)
        pick = Action("request_pickup", {"shipment_id": sid}, AGENT, "", f"request_pickup:{sid}")
        dock = Action("request_dock_slot", {"shipment_id": sid, "when": arrives.isoformat()},
                      AGENT, "", f"request_dock_slot:{sid}")
        what = ", ".join(f"{q:g} x {p}" for p, q in sorted(qtys.items())) or "goods"
        out.append(Candidate(
            id=pick.idempotency_key, title=f"Collect shipment {sid} from the port ({what})",
            actions=(pick, dock), effect=Effect(add_arrivals=arrivals), cost_eur=0.0,
            needs="site lead dock slot", parts=tuple(sorted(qtys))))
    return out


def _concessions(board: Board, tenant: dict, now: datetime, fc: Forecast, pi: PlanInput,
                 tz: ZoneInfo) -> list[Candidate]:
    site = tenant.get("site")
    inspections = _table(board, "inspection")
    parts = _table(board, "part")
    short_parts = {k for p, s in fc.shortfall.items() if s > _SHORT for k in pi.bom.get(p, {})}
    out: list[Candidate] = []
    # A quarantined batch is a stock qty fact post whose status is "quarantined".
    for post in board.query(kind="fact", status="quarantined", subject_prefix="stock/", limit=100_000):
        b = post.body
        if b.get("field") != "qty":
            continue
        s, _, rest = str(b.get("key", "")).partition("|")
        part, _, batch = rest.partition("|")
        qty = _num(b.get("value"))
        if s != site or not qty or qty <= 0:
            continue
        insp = inspections.get(batch)
        if insp is None or insp.get("result") != "failed":
            continue
        # Quality and Compliance has the last word on a revision that is not released.
        if board.query(kind="conflict", subject_prefix=f"revision_not_released/stock/{post.body['key']}/", limit=1):
            continue
        if str(insp.get("concession") or "").strip().lower() not in ("", "none", "no", "false", "n/a"):
            continue
        if part not in short_parts and fc.shadow_price.get(part, 0.0) <= 0:
            continue
        akey = f"request_concession:{batch}"
        params = {"gr_number": batch, "part_id": part, "qty": qty,
                  "category": _category(parts, part), "defect_rate": insp.get("defect_rate")}
        action = Action("request_concession", params, AGENT, "", akey)
        out.append(Candidate(
            id=akey, title=f"Request a concession for failed batch {batch}: {qty:g} x {part}",
            actions=(action,), effect=Effect(add_stock={part: qty}), cost_eur=0.0,
            needs="concession sign-off from quality and the category approver", parts=(part,)))
    return out


def _terms_holds(board: Board, tenant: dict, now: datetime, fc: Forecast, pi: PlanInput,
                 tz: ZoneInfo) -> list[Candidate]:
    site = str(tenant.get("site", ""))
    terms = _table(board, "contract_term")
    shipments = _table(board, "shipment")
    po_lines = _table(board, "po_line")
    have = {a.ref for a in pi.arrivals}
    seen: set[tuple[str, str]] = set()
    out: list[Candidate] = []
    for key, f in sorted(_table(board, "order_confirmation").items()):
        reason = str(f.get("hold_reason") or "").strip()
        if not reason:
            continue
        po = key.partition("|")[0]
        supplier = str(f.get("supplier_id") or "")
        mine = {k.partition("|")[2]: v.get("value") for k, v in terms.items()
                if k.partition("|")[0] == supplier and k.partition("|")[2] not in _NOT_TERMS}
        term = next((t for t in sorted(mine) if t.lower() in reason.lower()
                     or t.lower().replace("_", " ") in reason.lower()), "")
        if not term and mine:
            term = sorted(mine)[0]
        if (po, term) in seen:
            continue
        seen.add((po, term))
        akey = f"confirm_terms:{po}|{term}"
        action = Action("confirm_terms", {"supplier_id": supplier, "po_number": po, "term": term,
                                          "value": mine.get(term)}, AGENT, "", akey)
        # The held goods count only if the planner does not already count them.
        arrivals: list[Arrival] = []
        for sid, sf in shipments.items():
            if sid in have or po not in _as_list(sf.get("po_numbers")) or sf.get("status") in _DONE:
                continue
            when = _parse_dt(sf.get("eta_site"), tz)
            if when is None:
                when = _parse_dt(sf.get("eta"), tz)
                dest = sf.get("destination")
                if when is not None and dest and dest != site:
                    when += timedelta(days=transit_days(board, str(dest), site))
            if when is None:
                continue
            idx = hour_index(pi.hours, when, tz)
            arrivals += [Arrival(p, q, idx, 1.0, sid) for p, q in ship_part_qtys(sf, po_lines).items()]
        out.append(Candidate(
            id=akey, title=f"Confirm terms for order {po} ({term or 'terms'}): {reason}",
            actions=(action,), effect=Effect(add_arrivals=tuple(arrivals)), cost_eur=0.0,
            needs="written confirmation of the contract term",
            parts=tuple(sorted({a.part_id for a in arrivals}))))
    return out


def _priority(board: Board, tenant: dict, now: datetime, fc: Forecast, pi: PlanInput,
              tz: ZoneInfo) -> list[Candidate]:
    values = {p: float(v) for p, v in tenant.get("value_per_copy", {}).items()}
    prods = [p for p in pi.products if p in values]
    contested = {k for k, s in fc.shadow_price.items() if s > 0
                 and sum(1 for p in prods if pi.bom.get(p, {}).get(k, 0) > 0) >= 2}
    sharing = [p for p in prods if any(pi.bom.get(p, {}).get(k, 0) > 0 for k in contested)]
    if len({values[p] for p in sharing}) < 2:
        return []
    order = tuple(sorted(sharing, key=lambda p: (-values[p], p)))
    akey = "set_priority:" + ",".join(order)
    action = Action("set_priority", {"order": list(order)}, AGENT, "", akey)
    return [Candidate(
        id=akey, title="Give shared parts to " + " before ".join(order), actions=(action,),
        effect=Effect(priority=order), cost_eur=0.0, needs="line supervisor to follow the order",
        parts=tuple(sorted(contested)), always=True)]


def candidates(board: Board, tenant: dict, now: datetime, forecast: Forecast,
               plan_input: PlanInput) -> list[Candidate]:
    tz = ZoneInfo(tenant.get("timezone", "UTC"))
    now = now.astimezone(tz)
    taken = _taken_keys(board)
    out: list[Candidate] = []
    for gen in (_offers, _pickups, _concessions, _terms_holds, _priority):
        for c in gen(board, tenant, now, forecast, plan_input, tz):
            if not any(a.idempotency_key in taken for a in c.actions):
                out.append(c)
    return out


_PENDING = ("requested", "needs_approval", "allowed", "approved")


def assume_pending(board: Board, tenant: dict, now: datetime, pi: PlanInput) -> PlanInput:
    """Apply our pending actions to the plan input as assumed effects.

    Executed actions are already counted by `build_plan_input`.
    """
    tz = ZoneInfo(tenant.get("timezone", "UTC"))
    now = now.astimezone(tz)
    site = str(tenant.get("site", ""))
    shipments = _table(board, "shipment")
    po_lines = _table(board, "po_line")
    stock = dict(pi.stock)
    arrivals = list(pi.arrivals)
    for status in _PENDING:
        for post in board.query(kind="action", status=status, limit=100_000):
            kind, params = post.body.get("kind"), post.body.get("params", {})
            part, qty = params.get("part_id"), _num(params.get("qty"))
            if kind in ("accept_offer", "place_order") and part and qty:
                when = _parse_dt(params.get("arrives_at"), tz)
                if when is not None:
                    arrivals.append(Arrival(str(part), qty, hour_index(pi.hours, when, tz), 1.0, post.id))
            elif kind == "request_concession" and part and qty:
                stock[str(part)] = stock.get(str(part), 0.0) + qty
            elif kind == "request_pickup":
                sid = str(params.get("shipment_id"))
                f = shipments.get(sid)
                if f is not None:
                    arrivals += _pickup_arrivals(board, site, now, pi, tz, f, sid, po_lines)[2]
    return replace(pi, stock=stock, arrivals=tuple(arrivals))

"""Build a PlanInput from verified facts only.

Never reads files or claims. Quarantined facts are already excluded by
`board.facts()`.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from gtl.contracts import Arrival, Board, Fact, PlanInput

_DONE = ("delivered", "cancelled")


def _fact_table(board: Board, entity: str) -> dict[str, dict[str, Fact]]:
    out: dict[str, dict[str, Fact]] = defaultdict(dict)
    for f in board.facts(entity=entity):
        out[f.key][f.field] = f
    return out


def _table(board: Board, entity: str) -> dict[str, dict[str, Any]]:
    return {k: {fld: f.value for fld, f in fields.items()} for k, fields in _fact_table(board, entity).items()}


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).replace(";", ",").split(",") if s.strip()]


def _parse_dt(v: Any, tz: ZoneInfo) -> datetime | None:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.replace(tzinfo=tz) if t.tzinfo is None else t.astimezone(tz)


def _as_of_end(v: Any, tz: ZoneInfo) -> datetime | None:
    """The moment a reading describes. A time of exactly 00:00:00 means the source gave a date
    without a time, so it stands for the end of that day (the next midnight)."""
    t = _parse_dt(v, tz)
    if t is not None and (t.hour, t.minute, t.second, t.microsecond) == (0, 0, 0, 0):
        return t + timedelta(days=1)
    return t


def _packed_readings(board: Board, tz: ZoneInfo) -> dict[str, list[tuple[datetime, float]]]:
    """Per product, every packed_units reading (active or superseded), oldest first."""
    out: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for status in ("superseded", "active"):
        for p in board.query(kind="fact", status=status, subject_prefix="product/", limit=1_000_000):
            b = p.body
            t, v = _parse_dt(b.get("as_of"), tz), _num(b.get("value"))
            if b.get("field") == "packed_units" and not b.get("quarantined") and t is not None and v is not None:
                out[str(b["key"])].append((t, v))
    return {k: sorted(v, key=lambda r: r[0]) for k, v in out.items()}


def _packed_at(readings: list[tuple[datetime, float]], t: datetime) -> float | None:
    """The newest reading taken at or before `t`, None when there is none."""
    hit = [v for rt, v in readings if rt <= t]
    return hit[-1] if hit else None


def deadline_of(fact: Fact | None, tz: ZoneInfo) -> datetime | None:
    """A product's deadline. A bare date means the end of that day. A 'deadline' that is the very
    moment its own source was written is the time a status was taken, not a deadline, and is ignored."""
    if fact is None:
        return None
    d = _as_of_end(fact.value, tz)
    if d is not None and _parse_dt(fact.value, tz) == _parse_dt(fact.as_of, tz):
        return None
    return d


def _clock_on(date: str, hhmm: str, tz: ZoneInfo) -> datetime | None:
    try:
        h, m = (int(x) for x in str(hhmm).split(":")[:2])
        day = datetime.fromisoformat(date).date()
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(hours=h, minutes=m)


def _ceil_hour(t: datetime) -> datetime:
    floor = t.replace(minute=0, second=0, microsecond=0)
    return floor if floor == t else floor + timedelta(hours=1)


def _pack_hours(shifts: dict[str, dict[str, Any]], site: str, now: datetime,
                deadline: datetime | None, tz: ZoneInfo) -> list[datetime]:
    windows: list[tuple[datetime, datetime]] = []
    for key, fields in shifts.items():
        s, _, date = key.partition("|")
        if s != site:
            continue
        start = _clock_on(date, fields.get("start", ""), tz)
        end = _clock_on(date, fields.get("end", ""), tz)
        if start is None or end is None:
            continue
        if end <= start:
            end += timedelta(days=1)
        windows.append((start, end))
    first = _ceil_hour(now.astimezone(tz))
    hours: list[datetime] = []
    for start, end in sorted(windows):
        t = max(first, _ceil_hour(start))
        while t + timedelta(hours=1) <= end:
            if deadline is not None and t >= deadline:
                break
            hours.append(t)
            t += timedelta(hours=1)
    # Wall-clock hours in a shift are unique; guard against overlapping shifts.
    return sorted(set(hours))


def _first_hour_at_or_after(hours: list[datetime], t: datetime) -> int | None:
    for i, h in enumerate(hours):
        if h >= t:
            return i
    return None


def _known_bad(board: Board, site: str, part: str, active: dict[str, dict[str, Fact]], tz: ZoneInfo) -> float:
    """Units inside a part total that are known to be wrong or unusable, summed over the part's batches.

    A corrected booking counts as quarantined minus corrected value, a quarantined batch without a
    replacement counts whole, and a blocked batch counts whole.
    """
    latest: dict[str, float] = {}
    for p in board.query(kind="fact", status="quarantined", subject_prefix="stock/", limit=1_000_000):
        b = p.body
        s, _, rest = str(b.get("key", "")).partition("|")
        pt, _, batch = rest.partition("|")
        v = _num(b.get("value"))
        if b.get("field") == "qty" and (s, pt) == (site, part) and batch and v is not None:
            latest[str(b["key"])] = v
    bad = 0.0
    for key, q in latest.items():
        fixed = active.get(key, {}).get("qty")
        fixed_v = _num(fixed.value) if fixed else None
        bad += max(0.0, q - fixed_v) if fixed_v is not None else q
    for key, f in active.items():
        status, qty = f.get("quality_status"), f.get("qty")
        if key not in latest and status is not None and status.value == "blocked" and qty is not None:
            bad += _num(qty.value) or 0.0
    return bad


def _stock_levels(board: Board, site: str, tz: ZoneInfo
                  ) -> tuple[dict[str, float], dict[str, datetime], dict[str, dict[str, Any]]]:
    """Usable stock per part, the time it describes, and which parts were taken from a part total.

    A part total (a stock fact with an empty batch) newer than every batch fact is the basis,
    less what is known to be bad. Otherwise the usable batch facts are summed.
    """
    batches: dict[str, dict[str, dict[str, Fact]]] = defaultdict(dict)
    totals: dict[str, Fact] = {}
    for key, f in _fact_table(board, "stock").items():
        s, _, rest = key.partition("|")
        part, _, batch = rest.partition("|")
        if s != site or f.get("qty") is None or _num(f["qty"].value) is None:
            continue
        if batch:
            batches[part][key] = f
        else:
            totals[part] = f["qty"]
    stock: dict[str, float] = {}
    snapshot: dict[str, datetime] = {}
    basis: dict[str, dict[str, Any]] = {}
    for part in set(batches) | set(totals):
        # A steward's correction is dated when it was made, not when the stock was counted: it does not
        # make the batch readings newer than a part total.
        times = [t for f in batches[part].values()
                 if not any(str(c).startswith("steward:") or c == "carried_correction" for c in f["qty"].checks_passed)
                 and (t := _as_of_end(f["qty"].as_of, tz)) is not None]
        usable = [f for f in batches[part].values()
                  if f.get("quality_status") is None or f["quality_status"].value != "blocked"]
        total = totals.get(part)
        total_t = _as_of_end(total.as_of, tz) if total else None
        if total is not None and total_t is not None and (not times or total_t >= max(times)):
            bad = _known_bad(board, site, part, batches[part], tz)
            stock[part] = max(0.0, float(_num(total.value) or 0.0) - bad)
            snapshot[part] = total_t
            basis[part] = {"total": _num(total.value), "as_of": total.as_of, "known_bad": bad}
        elif usable:
            stock[part] = sum(_num(f["qty"].value) or 0.0 for f in usable)
            if times:
                snapshot[part] = max(times)
    return stock, snapshot, basis


def build_plan_input(board: Board, tenant: dict, now: datetime) -> PlanInput:
    tz = ZoneInfo(tenant.get("timezone", "UTC"))
    site = tenant["site"]
    now = now.astimezone(tz)

    # Products still to pack, and the earliest deadline.
    prod_facts = _fact_table(board, "product")
    prod = {k: {fld: f.value for fld, f in v.items()} for k, v in prod_facts.items()}
    products: dict[str, float] = {}
    deadlines: list[datetime] = []
    for pid, f in prod.items():
        demand, packed = _num(f.get("demand_units")), _num(f.get("packed_units"))
        if demand is None or packed is None:
            continue
        products[pid] = max(0.0, demand - packed)
        d = deadline_of(prod_facts.get(pid, {}).get("deadline"), tz)
        if d is not None:
            deadlines.append(d)
    deadline = min(deadlines) if deadlines else None

    # Parts that arrive inside another part are not planned separately.
    fitted = {k for k, f in _table(board, "part").items() if f.get("supplied_fitted_into")}
    bom: dict[str, dict[str, float]] = {p: {} for p in products}
    for key, f in _table(board, "bom").items():
        pid, _, part = key.partition("|")
        q = _num(f.get("qty_per"))
        if pid in bom and q is not None and part not in fitted:
            bom[pid][part] = q

    lines: dict[str, tuple[str, float]] = {}
    for key, f in _table(board, "line").items():
        s, _, line_id = key.partition("|")
        rate = _num(f.get("rate_per_hour"))
        if s == site and f.get("product_id") and rate is not None:
            lines[line_id] = (str(f["product_id"]), rate)

    hour_list = _pack_hours(_table(board, "shift"), site, now, deadline, tz)

    stock, snapshot, basis = _stock_levels(board, site, tz)
    snap = {part: min(snapshot.get(part, now), now) for part in stock}

    # Stock describes its snapshot time; take off what was packed since.
    readings = _packed_readings(board, tz)
    packed_now = {pid: _num(f.get("packed_units")) for pid, f in prod.items()}
    consumed: dict[str, float] = {}
    unknown: set[str] = set()
    for part, qty in list(stock.items()):
        used = 0.0
        for pid, lines_ in bom.items():
            per = lines_.get(part)
            now_v = packed_now.get(pid)
            if per is None or now_v is None:
                continue
            before = _packed_at(readings.get(pid, []), snap[part])
            if before is None:
                unknown.add(pid)
            else:
                used += max(0.0, now_v - before) * per
        if used > 0:
            consumed[part] = min(used, qty)
            stock[part] = max(0.0, qty - used)

    arrivals = _shipment_arrivals(board, site, now, hour_list, tz)
    arrivals += _action_arrivals(board, now, hour_list, tz, snap)

    values = {p: float(v) for p, v in tenant.get("value_per_copy", {}).items()}
    priority = tuple(sorted(products, key=lambda p: -values.get(p, 0.0)))
    return PlanInput(
        hours=tuple(h.isoformat() for h in hour_list),
        products=products,
        lines=lines,
        bom=bom,
        stock=dict(stock),
        arrivals=tuple(arrivals),
        value_per_copy={p: values[p] for p in products if p in values},
        priority=priority,
        consumed_since_snapshot=consumed,
        not_rolled_forward=tuple(sorted(unknown)),
        stock_basis=basis,
    )


def duplicate_shipments(ships: dict[str, dict[str, Any]]) -> set[str]:
    """Shipments recorded under their carrier reference while another record carries that reference.

    A plan sheet that lists inbound goods by container or air waybill number, next to the ERP's own
    shipment ids, describes the same goods twice. The record named after the reference is the copy.
    """
    return {sid for sid, f in ships.items()
            if str(f.get("reference") or sid) == sid
            and any(other != sid and str(g.get("reference") or "") == sid for other, g in ships.items())}


def _executed_actions(board: Board) -> list[dict[str, Any]]:
    return [p.body for p in board.query(kind="action", status="executed")]


def _shipment_arrivals(board: Board, site: str, now: datetime,
                       hours: list[datetime], tz: ZoneInfo) -> list[Arrival]:
    if not hours:
        return []
    routes = _table(board, "route")
    po_lines = _table(board, "po_line")
    picked_up = {
        str(a.get("params", {}).get("shipment_id"))
        for a in _executed_actions(board) if a.get("kind") == "request_pickup"
    }

    confirmed = {
        str(a.get("params", {}).get("po_number"))
        for a in _executed_actions(board) if a.get("kind") == "confirm_terms"
    }
    held = {key.partition("|")[0] for key, f in _table(board, "order_confirmation").items()
            if str(f.get("hold_reason") or "").strip()} - confirmed

    def transit_days(origin: str) -> float:
        for r in routes.values():
            if r.get("origin") == origin and r.get("destination") == site:
                days = _num(r.get("transit_days"))
                if days is not None:
                    return days
        return 1.0

    def po_qty(po_numbers: list[str], part: str) -> float | None:
        total, found = 0.0, False
        for key, f in po_lines.items():
            if key.partition("|")[0] in po_numbers and f.get("part_id") == part \
                    and f.get("status") not in _DONE and _num(f.get("qty")) is not None:
                total += float(f["qty"])
                found = True
        return total if found else None

    out: list[Arrival] = []
    ships = _table(board, "shipment")
    duplicates = duplicate_shipments(ships)
    for sid, f in ships.items():
        if sid in duplicates:
            continue
        status = f.get("status")
        if status in _DONE:
            continue
        if status == "arrived_port" and sid not in picked_up:
            continue
        if held & set(_as_list(f.get("po_numbers"))):
            continue  # the supplier holds it until the terms are confirmed
        when = _parse_dt(f.get("eta_site"), tz)
        if when is None:
            when = _parse_dt(f.get("eta"), tz)
            if when is None:
                continue
            dest = f.get("destination")
            if dest and dest != site:
                when += timedelta(days=transit_days(str(dest)))
        idx = _first_hour_at_or_after(hours, when)
        if idx is None:
            continue
        pos = _as_list(f.get("po_numbers"))
        parts = _as_list(f.get("part_ids"))
        if not parts:
            parts = sorted({str(v.get("part_id")) for k, v in po_lines.items()
                            if k.partition("|")[0] in pos and v.get("part_id")})
        direct = _num(f.get("qty_shipped"))
        if direct is None:
            direct = _num(f.get("qty"))
        for part in parts:
            if len(parts) == 1 and direct is not None:
                qty = direct
            else:
                qty = po_qty(pos, part)
            if qty is not None and qty > 0:
                out.append(Arrival(part, qty, idx, 1.0, sid))
    return out


def _action_arrivals(board: Board, now: datetime, hours: list[datetime],
                     tz: ZoneInfo, snap: dict[str, datetime]) -> list[Arrival]:
    out: list[Arrival] = []
    for post in board.query(kind="action", status="executed"):
        a = post.body
        if a.get("kind") not in ("accept_offer", "place_order") or not hours:
            continue
        params = a.get("params", {})
        when = _parse_dt(params.get("arrives_at"), tz)
        qty = _num(params.get("qty"))
        if when is None or qty is None or not params.get("part_id") or when <= snap.get(str(params["part_id"]), now):
            continue  # it landed before the stock was counted: already in the stock
        idx = _first_hour_at_or_after(hours, when)
        if idx is not None:
            out.append(Arrival(str(params["part_id"]), qty, idx, 1.0, post.id))
    return out

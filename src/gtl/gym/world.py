"""The Gym world: hidden truth, the book view, hourly steps and action effects.

Design: docs/gym.md sections 4 and 6. The world holds two views of the same
supply chain. The truth is what is physically there. The book is what the ERP
says. The packing floor trusts the book, but physical parts decide what can be
packed, so a wrong booking shows up as a line that runs dry.

Pending effects are callables taking the world. They must close over plain
data only (ids, numbers, strings), never over a world, so that
`copy.deepcopy(world)` gives an independent copy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from gtl.contracts import Action, Claim, Evidence, GateVerdict, make_key

if TYPE_CHECKING:  # avoid an import cycle: disruptions imports this module
    from gtl.gym.disruptions import Remedy

HOUR = timedelta(hours=1)


@dataclass
class Batch:
    """One stock batch at the site."""

    part_id: str
    batch: str
    qty: float  # truth
    book_qty: float  # what the ERP says
    quality: str = "ok"  # truth: "ok" | "failed"
    book_quality: str = "unrestricted"  # "unrestricted" | "blocked"
    revision: str | None = None
    concession: bool = False  # a person accepted the failed lot
    quarantined: bool = False


@dataclass
class Shipment:
    shipment_id: str
    supplier_id: str
    part_id: str
    qty: float  # truth
    book_qty: float
    arrives_at: datetime | None  # truth; None while something blocks it
    book_eta: datetime | None  # arrival at the site, as the book sees it
    needs: set[str] = field(default_factory=set)  # subset of {"pickup", "dock_slot", "terms"}
    feed_frozen: bool = False
    road_hours: float = 0.0  # last leg from the airport or port to the site
    mode: str = ""
    reference: str = ""
    origin: str = ""
    destination: str = ""
    book_status: str = "in_transit"
    po_numbers: list[str] = field(default_factory=list)
    revision: str | None = None
    delivered: bool = False


@dataclass
class Supplier:
    supplier_id: str
    language: str
    spare: dict[str, float] = field(default_factory=dict)  # part_id -> finished stock it could ship
    offer: dict[str, dict] = field(default_factory=dict)  # part_id -> {qty, unit_price, ready_hours, transit_hours, surcharge_pct}
    released: bool = True
    response_hours: float = 4.0
    forthcoming: float = 1.0
    honest: bool = True
    name: str = ""
    contact: str = ""
    release_status: str = "qualified"
    parts: dict[str, dict] = field(default_factory=dict)  # part_id -> {unit_price, currency, lead_time_days, share}
    on_time_rate: float = 1.0  # share of deliveries that arrive when promised; visible to the agents
    slip_hours: float = 0.0  # how late a slipped delivery is; not visible


@dataclass
class Person:
    role_id: str
    vacant: bool
    response_hours: float
    reports_to: str | None = None
    title: str = ""
    person: str | None = None


@dataclass
class WorldState:
    now: datetime
    deadline: datetime
    shifts: list[tuple[datetime, datetime]]
    lines: dict[str, tuple[str, float]]  # line_id -> (product_id, rate_per_hour)
    bom: dict[str, dict[str, float]]
    to_pack: dict[str, float]
    packed: dict[str, float]
    batches: list[Batch]
    shipments: list[Shipment]
    suppliers: dict[str, Supplier]
    people: dict[str, Person]
    priority: list[str]  # product ids; first gets shared parts first
    pending: list[tuple[datetime, Callable]] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)
    truth: dict = field(default_factory=dict)
    # Extras beyond the spec:
    site: str = ""
    timezone: str = "Europe/Berlin"
    start: datetime | None = None
    products: dict[str, dict] = field(default_factory=dict)
    parts: dict[str, dict] = field(default_factory=dict)
    fitted_into: dict[str, str] = field(default_factory=dict)
    fx_to_eur: dict[str, float] = field(default_factory=lambda: {"EUR": 1.0})
    packed_start: dict[str, float] = field(default_factory=dict)
    allowed_revisions: dict[str, list[str]] = field(default_factory=dict)  # part_id -> allowed; absent means any
    reserve: bool = False  # set by a reallocation: lower products may not eat parts a higher one still needs
    tenant: dict = field(default_factory=dict)

    # -- small helpers -----------------------------------------------------
    def remaining(self, product_id: str) -> float:
        """Copies of a product still to pack in this run."""
        done = self.packed.get(product_id, 0.0) - self.packed_start.get(product_id, 0.0)
        return max(0.0, self.to_pack.get(product_id, 0.0) - done)

    def find_batch(self, part_id: str, batch: str) -> Batch | None:
        for b in self.batches:
            if b.part_id == part_id and b.batch == batch:
                return b
        return None

    def find_shipment(self, shipment_id: str) -> Shipment | None:
        for s in self.shipments:
            if s.shipment_id == shipment_id:
                return s
        return None

    def in_shift(self, at: datetime | None = None) -> bool:
        at = at or self.now
        return any(a <= at < b for a, b in self.shifts)

    def route_role(self, role_id: str | None) -> Person | None:
        """First filled role at or above `role_id` along `reports_to`."""
        seen: set[str] = set()
        while role_id and role_id not in seen:
            seen.add(role_id)
            person = self.people.get(role_id)
            if person is None:
                return None
            if not person.vacant:
                return person
            role_id = person.reports_to
        return None

    def to_eur(self, amount: float, currency: str) -> float:
        if currency not in self.fx_to_eur:
            raise ValueError(f"no EUR rate for currency {currency!r}; the Gym cannot price it")
        return amount * self.fx_to_eur[currency]

    def schedule(self, delay_hours: float, fn: Callable[["WorldState"], None]) -> None:
        self.pending.append((self.now + timedelta(hours=delay_hours), fn))

    def log(self, kind: str, **fields: Any) -> dict:
        entry = {"kind": kind, "at": self.now.isoformat(), **fields}
        self.ledger.append(entry)
        return entry

    def digest(self) -> tuple:
        """A compact comparable summary of the physical and money outcome."""
        return (
            tuple(sorted(self.packed.items())),
            tuple(sorted((b.part_id, b.batch, b.qty, b.book_qty, b.quarantined, b.concession) for b in self.batches)),
            tuple(sorted((e["kind"], round(e.get("eur", e.get("qty", 1.0)), 6)) for e in self.ledger
                         if e["kind"] in ("spend", "bad_units", "violation", "human_item", "email", "fraud"))),
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
RELIABILITY_LEVELS = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95)
RELIABILITY_SALT = "trait"  # chosen so the base fixture has suppliers at or below 0.6 and at or above 0.9 that share parts


def derived_reliability(supplier_id: str) -> tuple[float, float]:
    """A supplier's stable (on_time_rate, slip_hours) from its id alone: the same in every scenario."""
    h = int(hashlib.sha256(f"{RELIABILITY_SALT}:{supplier_id}".encode()).hexdigest(), 16)
    return RELIABILITY_LEVELS[h % len(RELIABILITY_LEVELS)], float(18 + (h // len(RELIABILITY_LEVELS)) % 19)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_base(source: str | Path | dict) -> WorldState:
    """Build a world from the base snapshot. Truth equals the book initially."""
    if isinstance(source, dict):
        d = copy.deepcopy(source)
    else:
        d = json.loads(Path(source).read_text(encoding="utf-8"))
    start = _dt(d["start"])
    batches = [Batch(part_id=b["part_id"], batch=b["batch"], qty=float(b["qty"]), book_qty=float(b["qty"]),
                     revision=b.get("revision")) for b in d["batches"]]
    shipments = []
    for s in d["shipments"]:
        book_eta = _dt(s["eta"]) + timedelta(days=s.get("road_days", 0.0))
        needs = set(s.get("needs", []))
        shipments.append(Shipment(
            shipment_id=s["shipment_id"], supplier_id=s["supplier_id"], part_id=s["part_id"],
            qty=float(s["qty"]), book_qty=float(s["qty"]),
            arrives_at=None if needs else book_eta, book_eta=book_eta, needs=needs,
            road_hours=24.0 * s.get("road_days", 0.0), mode=s.get("mode", ""),
            reference=s.get("reference", ""), origin=s.get("origin", ""),
            destination=s.get("destination", ""),
            book_status="arrived_port" if "pickup" in needs else s.get("status", "in_transit"),
            po_numbers=list(s.get("po_numbers", []))))
    suppliers = {}
    for s in d["suppliers"]:
        suppliers[s["supplier_id"]] = Supplier(
            supplier_id=s["supplier_id"], language=s["language"], released=s["released"],
            name=s.get("name", ""), contact=s.get("contact", ""),
            release_status=s.get("release_status", "qualified"), parts=copy.deepcopy(s.get("parts", {})))
        rate, slip = derived_reliability(s["supplier_id"])
        suppliers[s["supplier_id"]].on_time_rate = float(s.get("on_time_rate", rate))
        suppliers[s["supplier_id"]].slip_hours = float(s.get("slip_hours", slip))
    people = {p["role_id"]: Person(p["role_id"], p["vacant"], float(p["response_hours"]), p.get("reports_to"),
                                   p.get("title", ""), p.get("person")) for p in d["people"]}
    lines = {lid: (v["product_id"], float(v["rate_per_hour"])) for lid, v in d["lines"].items()}
    to_pack = {k: float(v) for k, v in d["to_pack"].items()}
    packed = {k: float(v) for k, v in d["packed"].items()}
    world = WorldState(
        now=start, deadline=_dt(d["deadline"]),
        shifts=[(_dt(s["start"]), _dt(s["end"])) for s in d["shifts"]],
        lines=lines, bom={p: dict(v) for p, v in d["bom"].items()},
        to_pack=to_pack, packed=packed, batches=batches, shipments=shipments,
        suppliers=suppliers, people=people, priority=list(d["products"].keys()),
        site=d["site"], timezone=d.get("timezone", "Europe/Berlin"), start=start,
        products=copy.deepcopy(d["products"]), parts=copy.deepcopy(d.get("parts", {})),
        fitted_into=dict(d.get("fitted_into", {})), fx_to_eur=dict(d.get("fx_to_eur", {"EUR": 1.0})),
        packed_start=dict(packed))
    used = {p.get("currency") for sup in suppliers.values() for p in sup.parts.values()}
    used |= {p.get("currency") for p in world.parts.values()}
    unknown = sorted(str(c) for c in used if c and c not in world.fx_to_eur)
    if unknown:
        raise ValueError(f"scenario uses currencies with no EUR rate: {', '.join(unknown)}")
    world.truth = {"labels": [], "remedies": [], "observables": [], "applied": set(), "claimed": set()}
    return world


# ---------------------------------------------------------------------------
# Hourly step
# ---------------------------------------------------------------------------
def _fire_pending(world: WorldState) -> None:
    while True:
        due = [p for p in world.pending if p[0] <= world.now]
        if not due:
            return
        first = min(due, key=lambda p: p[0])  # stable: earliest first, then insertion order
        for i, p in enumerate(world.pending):
            if p is first:
                del world.pending[i]
                break
        first[1](world)


def _deliver(world: WorldState) -> None:
    for s in world.shipments:
        if not s.delivered and s.arrives_at is not None and s.arrives_at <= world.now:
            s.delivered = True
            world.batches.append(Batch(part_id=s.part_id, batch=f"IN-{s.shipment_id}", qty=s.qty,
                                       book_qty=s.qty, revision=s.revision))
            world.log("arrival", shipment_id=s.shipment_id, part_id=s.part_id, qty=s.qty)


def _usable(world: WorldState, part_id: str) -> list[Batch]:
    return [b for b in world.batches if b.part_id == part_id and b.qty > 0 and not b.quarantined
            and b.book_quality == "unrestricted"]


def _shift_hours_left(world: WorldState) -> float:
    """Hours of shift between now and the deadline."""
    end = world.deadline
    return sum(max(0.0, (min(b, end) - max(a, world.now)).total_seconds() / 3600.0) for a, b in world.shifts)


def _reserved_for_higher(world: WorldState, product_id: str, part_id: str) -> float:
    """Parts held back for higher products: what they still need and can still pack before the deadline."""
    if not world.reserve:
        return 0.0
    hours = _shift_hours_left(world)
    total = 0.0
    for other in world.priority:
        if other == product_id:
            break
        rate = sum(r for pid, r in world.lines.values() if pid == other)
        total += min(world.remaining(other), rate * hours) * world.bom.get(other, {}).get(part_id, 0.0)
    return total


def _pack_line(world: WorldState, product_id: str, rate: float) -> None:
    bom = world.bom.get(product_id, {})
    if not bom:
        return
    n = min(rate, world.remaining(product_id))
    for part_id, per in bom.items():
        have = sum(b.qty for b in _usable(world, part_id)) - _reserved_for_higher(world, product_id, part_id)
        n = min(n, math.floor(max(0.0, have) / per + 1e-9))
    if n <= 0:
        return
    for part_id, per in bom.items():
        need = n * per
        for b in _usable(world, part_id):  # list order: stable
            if need <= 0:
                break
            take = min(need, b.qty)
            b.qty -= take
            b.book_qty = max(0.0, b.book_qty - take)
            need -= take
            wrong_rev = (b.revision is not None and part_id in world.allowed_revisions
                         and b.revision not in world.allowed_revisions[part_id])
            if (b.quality == "failed" and not b.concession) or wrong_rev:
                world.log("bad_units", qty=take / per, part_id=part_id, batch=b.batch,
                          reason="wrong_revision" if wrong_rev else "failed_lot")
    world.packed[product_id] = world.packed.get(product_id, 0.0) + n


def step_hour(world: WorldState) -> None:
    """Fire due effects, pack inside shifts, advance one hour."""
    _fire_pending(world)
    _deliver(world)
    if world.in_shift():
        order = {p: i for i, p in enumerate(world.priority)}
        lines = sorted(world.lines.values(), key=lambda v: order.get(v[0], len(order)))
        for product_id, rate in lines:
            _pack_line(world, product_id, rate)
    world.now += HOUR


def run_to_deadline(world: WorldState) -> WorldState:
    while world.now < world.deadline:
        step_hour(world)
    return world


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def _per_item(action: Action) -> list[Action]:
    """A data fix request lists several records; each one is its own action for the remedies."""
    items = action.params.get("items") or []
    return [replace(action, params={**action.params, **item, "items": None}) for item in items]


def matches_remedy(remedy: "Remedy", action: Action) -> bool:
    if action.kind == "data_fix_request" and action.params.get("items"):
        return any(matches_remedy(remedy, a) for a in _per_item(action))
    for key, want in remedy.match.items():
        if key == "kind":
            if action.kind != want:
                return False
        elif isinstance(want, (list, tuple, set)):  # any of several accepted spellings
            if str(action.params.get(key)) not in {str(w) for w in want}:
                return False
        elif str(action.params.get(key)) != str(want):
            return False
    return True


def _stage_commit(world: WorldState, remedy: "Remedy", params: dict) -> None:
    """The decision is made: book the spend and emails, then let the physical delay run."""
    if remedy.offer:
        o = remedy.offer
        promised = world.now + timedelta(hours=o["promised_hours"])
        late = world.truth.get("slips", {}).get(o["supplier_id"], 0.0)
        world.log("offer_outcome", remedy_id=remedy.id, supplier_id=o["supplier_id"], part_id=o["part_id"],
                  promised_arrival=promised.isoformat(), actual_arrival=(promised + timedelta(hours=late)).isoformat(),
                  late_hours=late, supplier_on_time_rate=world.suppliers[o["supplier_id"]].on_time_rate)
    if remedy.cost_eur:
        world.log("spend", eur=remedy.cost_eur, remedy_id=remedy.id)
    for _ in range(remedy.emails):
        world.log("email", remedy_id=remedy.id)
    if remedy.delay_hours <= 0:
        remedy.effect(world, params)
    else:
        world.schedule(remedy.delay_hours, lambda w, r=remedy, p=params: r.effect(w, p))


def commit_remedy(world: WorldState, remedy: "Remedy", params: dict | None = None,
                  approval_hours: float = 0.0, count_human: bool = True) -> None:
    """Apply a remedy: human items now, spend and effect after the approval time and delay."""
    p = {**remedy.params, **(params or {})}
    world.truth["applied"].add(remedy.id)
    if count_human:
        for _ in range(remedy.human_items):
            world.log("human_item", remedy_id=remedy.id)
    if approval_hours > 0:
        world.schedule(approval_hours, lambda w, r=remedy, q=p: _stage_commit(w, r, q))
    else:
        _stage_commit(world, remedy, p)


def _net_value(world: WorldState, remedy: "Remedy", params: dict, tenant: dict) -> float:
    """Reward change from granting this remedy (with the ones that only work together with it)."""
    from gtl.gym.score import reward_of  # local import: score imports this module

    by_id = {r.id: r for r in world.truth.get("remedies", [])}
    group = [remedy] + [by_id[i] for i in remedy.peers if i in by_id]
    base = copy.deepcopy(world)
    with_it = copy.deepcopy(world)
    for r in group:
        commit_remedy(with_it, r, params if r is remedy else None, 0.0, count_human=False)
    run_to_deadline(base)
    run_to_deadline(with_it)
    return reward_of(with_it, tenant) - reward_of(base, tenant)


def apply_action(world: WorldState, action: Action, verdict: GateVerdict, *,
                 tenant: dict | None = None, human_done: bool = False) -> dict:
    """Effects of an executed action (docs/gym.md section 6). Returns a small status dict.

    `human_done` means a person already approved through a decision card: the card was counted
    and the approval time has passed, so neither is added again.
    """
    entry = {"action_kind": action.kind, "idempotency_key": action.idempotency_key, "outcome": verdict.outcome}
    if verdict.outcome == "blocked":
        world.log("violation", rule_id=verdict.rule_id, **entry)
        world.log("action", remedy_id=None, status="blocked", **entry)
        return {"status": "blocked"}

    if action.kind == "send_email":
        world.log("email", to=action.params.get("to"))
        sup = next((s for s in world.suppliers.values()
                    if s.supplier_id == action.params.get("supplier_id") or
                    (s.contact and s.contact == action.params.get("to"))), None)
        if sup is not None:
            sid = sup.supplier_id
            world.schedule(sup.response_hours, lambda w, sid=sid: w.log(
                "reply_due", supplier_id=sid, fact_sheet=fact_sheet(w, sid)))
        world.log("action", remedy_id=None, status="email", **entry)
        return {"status": "email_sent", "reply_due_hours": sup.response_hours if sup else None}

    if action.kind == "data_fix_request" and len(action.params.get("items") or []) > 1:
        results = [apply_action(world, a, verdict, tenant=tenant, human_done=human_done) for a in _per_item(action)]
        return next((r for r in results if r["status"] != "no_effect"), results[0])
    applied = world.truth["applied"]
    candidates = [r for r in world.truth.get("remedies", []) if matches_remedy(r, action)]
    remedy = next((r for r in candidates if r.id not in applied), None)
    if remedy is None:
        status = "duplicate" if candidates else _generic_effect(world, action)
        world.log("action", remedy_id=None, status=status, **entry)
        return {"status": status}

    tenant = tenant or world.tenant
    needs_approval = verdict.outcome == "needs_approval"
    human = needs_approval or remedy.approver_role is not None
    params = {**remedy.params, **{k: v for k, v in action.params.items() if k == "order"}}
    approval_hours = 0.0
    approver = None
    if human:
        approver = world.route_role(verdict.approver_role or remedy.approver_role)
        approval_hours = 0.0 if human_done else (approver.response_hours if approver else 0.0)
    if needs_approval and remedy.cost_eur > 0 and tenant:
        approved = _net_value(world, remedy, params, tenant) > 0
    else:
        approved = True  # free requests are granted
    if not human_done and (needs_approval or remedy.human_items):
        world.log("human_item", remedy_id=remedy.id, approver=approver.role_id if approver else None)
    world.log("action", remedy_id=remedy.id, disruption_id=remedy.disruption_id,
              status="scheduled" if approved else "rejected", **entry)
    world.truth["applied"].add(remedy.id)
    if not approved:
        world.log("approval", remedy_id=remedy.id, approved=False)
        return {"status": "rejected", "remedy_id": remedy.id}
    if human and not human_done:
        world.log("approval", remedy_id=remedy.id, approved=True,
                  role=approver.role_id if approver else None, hours=approval_hours)
    commit_remedy(world, remedy, params, approval_hours, count_human=False)
    return {"status": "scheduled", "remedy_id": remedy.id, "approval_hours": approval_hours,
            "effective_at": (world.now + timedelta(hours=approval_hours + remedy.delay_hours)).isoformat()}


def _generic_effect(world: WorldState, action: Action) -> str:
    """Actions with a plain world effect even when no disruption offers them."""
    p = action.params
    if action.kind == "quarantine_batch":
        b = world.find_batch(str(p.get("part_id")), str(p.get("batch")))
        if b is not None:
            b.quarantined = True
            return "quarantined"
    if action.kind == "set_priority":
        order = [x for x in p.get("order", []) if x in world.products]
        if order:
            world.priority = order + [x for x in world.priority if x not in order]
            return "priority_set"
    return "no_effect"


# ---------------------------------------------------------------------------
# Views: what a supplier knows, what the book says
# ---------------------------------------------------------------------------
def fact_sheet(world: WorldState, supplier_id: str) -> dict:
    """Only what this supplier would know."""
    sup = world.suppliers[supplier_id]
    ships = []
    for s in world.shipments:
        if s.supplier_id != supplier_id:
            continue
        ships.append({"shipment_id": s.shipment_id, "part_id": s.part_id, "qty_shipped": s.qty,
                      "qty_ordered": s.book_qty, "delivered": s.delivered,
                      "arrives_at": s.arrives_at.isoformat() if s.arrives_at else None,
                      "holds": sorted(h for h in s.needs if h == "terms")})
    return {
        "supplier_id": supplier_id, "name": sup.name, "language": sup.language, "released": sup.released,
        "spare_stock": dict(sup.spare), "offers": copy.deepcopy(sup.offer), "shipments": ships,
        "holds": [x["shipment_id"] for x in ships if x["holds"]],
        "forthcoming": sup.forthcoming, "honest": sup.honest,
    }


def book_claims(world: WorldState) -> list[Claim]:
    """The book view as claims in the canonical vocabulary."""
    as_of = world.now.isoformat()
    ev = (Evidence(source="sim:book"),)
    out: list[Claim] = []

    def add(entity: str, key: str, fld: str, value: Any, unit: str | None = None) -> None:
        out.append(Claim(entity=entity, key=key, field=fld, value=value, as_of=as_of, source_kind="sim",
                         evidence=ev, author="sim", unit=unit))

    for b in world.batches:
        key = make_key(world.site, b.part_id, b.batch)
        add("stock", key, "qty", b.book_qty, "PC")
        add("receipt", b.batch, "delivery_note", f"DN-{b.batch}")  # the receipt names its delivery note
        add("stock", key, "quality_status", b.book_quality)
        if b.revision is not None:
            add("stock", key, "revision", b.revision)
    for s in world.shipments:
        if s.delivered:
            add("shipment", s.shipment_id, "status", "delivered")
            continue
        add("shipment", s.shipment_id, "supplier_id", s.supplier_id)
        add("shipment", s.shipment_id, "part_ids", [s.part_id])
        add("shipment", s.shipment_id, "qty", s.book_qty, "PC")
        add("shipment", s.shipment_id, "mode", s.mode)
        if s.po_numbers:
            add("shipment", s.shipment_id, "po_numbers", list(s.po_numbers))
        add("shipment", s.shipment_id, "status", s.book_status)
        if s.book_eta is not None:
            add("shipment", s.shipment_id, "eta_site", s.book_eta.isoformat())
    for pid, info in world.products.items():
        add("product", pid, "name", info.get("name"))
        add("product", pid, "demand_units", info.get("demand_units"))
        add("product", pid, "preorder_units", info.get("preorder_units"))
        add("product", pid, "packed_units", world.packed.get(pid, 0.0))
        add("product", pid, "deadline", world.deadline.isoformat())
    for pid, parts in world.bom.items():
        for part_id, per in parts.items():
            add("bom", make_key(pid, part_id), "qty_per", per)
    for lid, (pid, rate) in world.lines.items():
        key = make_key(world.site, lid)
        add("line", key, "product_id", pid)
        add("line", key, "rate_per_hour", rate)
    for a, b in world.shifts:
        key = make_key(world.site, a.date().isoformat())
        add("shift", key, "start", a.strftime("%H:%M"))
        add("shift", key, "end", b.strftime("%H:%M"))
    for sup in world.suppliers.values():
        add("supplier", sup.supplier_id, "name", sup.name)
        add("supplier", sup.supplier_id, "contact_email", sup.contact)
        add("supplier", sup.supplier_id, "language", sup.language)
        add("supplier", sup.supplier_id, "release_status", sup.release_status)
        add("contract_term", make_key(sup.supplier_id, "on_time_rate"), "value", sup.on_time_rate)
    for p in world.people.values():
        add("role", p.role_id, "title", p.title)
        add("role", p.role_id, "vacant", p.vacant)
        add("role", p.role_id, "reports_to", p.reports_to)
        if p.person:
            add("role", p.role_id, "person", p.person)
    return out


def _disputed(world: WorldState, key: str) -> bool:
    """A stock record whose book is known to be wrong. The ERP feed leaves it to the data steward's correction."""
    _, part_id, batch = (key.split("|") + ["", ""])[:3]
    b = world.find_batch(part_id, batch)
    return b is not None and (b.book_qty != b.qty or (b.quality == "failed" and not b.concession))


def refresh_claims(world: WorldState, sent: dict[tuple[str, str, str], Any]) -> list[Claim]:
    """What the ERP would add now: every book value that changed since the last call, as claims.

    `sent` remembers the last value per slot and is updated. Packing, arrivals and corrections show up
    here, so the agents plan on a book that moves with the warehouse. The stock of a disputed batch
    is not refreshed: the book is wrong for it, and the steward's correction stands until its owner fixes it.
    """
    out: list[Claim] = []
    for claim in book_claims(world):
        if claim.entity == "stock" and claim.slot in sent and _disputed(world, claim.key):
            continue
        if sent.get(claim.slot, object()) == claim.value:
            continue
        sent[claim.slot] = claim.value
        out.append(claim)
    return out

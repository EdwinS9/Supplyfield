"""Typed disruptions (docs/gym.md section 5).

Each class changes the truth and/or the book, says what the agents can see and
when, offers remedies, and states the ground truth as labels. Targets and
parameters come from the world and a seeded `random.Random`; no class names a
specific part, supplier or shipment.

An instance holds plain data only, so it round-trips through `to_dict` and
`DISRUPTIONS[type](**params)`.
"""
from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable, ClassVar, Protocol

from gtl.contracts import Claim, Evidence, make_key
from gtl.gym.world import Shipment, WorldState

DAY = timedelta(days=1)
BOOK_SOURCES = ("erp", "tracker", "offer")  # systems and mail: no document quote is expected


@dataclass(frozen=True)
class Remedy:
    """One thing a person or agent can do about a disruption."""

    id: str
    disruption_id: str
    match: dict  # the action that triggers it: {"kind": ..., plus key params}; a list value accepts any of its items
    effect: Callable[[WorldState, dict], None]  # what it does to the world, called after the delay
    cost_eur: float = 0.0
    delay_hours: float = 0.0  # physical delay between the decision and the effect
    approver_role: str | None = None  # a person who must answer; routed up when the role is vacant
    params: dict = field(default_factory=dict)  # defaults used when no action supplies them (the oracle)
    peers: tuple[str, ...] = ()  # remedy ids that only work together with this one
    human_items: int = 0  # decision cards or data-fix requests this causes
    emails: int = 0
    offer: dict | None = None  # an accepted offer: supplier_id, part_id, promised_hours (before any slip)


class Disruption(Protocol):
    type: ClassVar[str]

    @classmethod
    def sample(cls, world: WorldState, rng: random.Random) -> "Disruption | None": ...
    def apply(self, world: WorldState, rng: random.Random | None = None) -> None: ...
    def observables(self, world: WorldState) -> list[tuple[datetime, Claim]]: ...
    def remedies(self, world: WorldState) -> list[Remedy]: ...
    def labels(self) -> dict: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bom_parts(world: WorldState) -> set[str]:
    return {p for parts in world.bom.values() for p in parts}


def _claimed(world: WorldState) -> set:
    return world.truth.setdefault("claimed", set())


def _obs(world: WorldState, release: datetime, entity: str, key: str, fld: str, value: Any, ev: str,
         flags: tuple[str, ...] = (), quote: str | None = None, unit: str | None = None) -> tuple[datetime, Claim]:
    if quote is None and ev.partition(":")[0] not in BOOK_SOURCES:
        quote = f"{fld}: {value}"  # a document reader always quotes the line it read
    claim = Claim(entity=entity, key=key, field=fld, value=value, as_of=release.isoformat(), source_kind="sim",
                  evidence=(Evidence(source=f"sim:{ev}", quote=quote),), author="sim", unit=unit, flags=flags)
    return release, claim


def _arrival(s: Shipment) -> datetime | None:
    return s.arrives_at or s.book_eta


def _shipment_targets(world: WorldState, rng: random.Random, kind: str) -> Shipment | None:
    """A shipment of a part the products need, not yet touched. Prefer ones landing in the window."""
    claimed = _claimed(world)
    cands = [s for s in world.shipments
             if not s.delivered and not s.needs and s.part_id in _bom_parts(world)
             and ("shipment", s.shipment_id) not in claimed and s.qty >= 1000]
    if kind == "port":
        cands = [s for s in cands if s.road_hours > 0]
    window = [s for s in cands if _arrival(s) is not None
              and world.start - DAY <= _arrival(s) <= world.deadline + DAY]
    pool = window or cands
    return rng.choice(pool) if pool else None


def _alt_supplier(world: WorldState, part_id: str, rng: random.Random, exclude: str | None = None) -> str | None:
    cands = sorted(s.supplier_id for s in world.suppliers.values()
                   if s.released and part_id in s.parts and s.supplier_id != exclude)
    return rng.choice(cands) if cands else None


def _add_offer(world: WorldState, supplier_id: str, part_id: str, qty: float,
               ready_hours: float, transit_hours: float, price: tuple[float, str] | None = None) -> None:
    sup = world.suppliers[supplier_id]
    unit_price, currency = price or (sup.parts[part_id]["unit_price"], sup.parts[part_id]["currency"])
    sup.spare[part_id] = sup.spare.get(part_id, 0.0) + qty
    sup.offer[part_id] = {
        "qty": qty, "unit_price": unit_price, "currency": currency,
        "ready_hours": ready_hours, "transit_hours": transit_hours, "surcharge_pct": 0.0,
        "valid_until": (world.now + DAY).isoformat(),
    }


def _offer_claim(world: WorldState, supplier_id: str, part_id: str, release: datetime) -> list[tuple[datetime, Claim]]:
    o = world.suppliers[supplier_id].offer[part_id]
    key = make_key(supplier_id, f"OFFER-{part_id}", 1)
    ev = f"offer:{supplier_id}"
    out = [_obs(world, release, "offer", key, "part_id", part_id, ev)]
    for f in ("qty", "unit_price", "currency", "surcharge_pct", "ready_hours", "transit_hours", "valid_until"):
        out.append(_obs(world, release, "offer", key, f, o[f], ev))
    return out


def _receive_order(supplier_id: str, part_id: str, qty: float, tag: str) -> Callable[[WorldState, dict], None]:
    """Effect: the supplier ships spare stock; it lands when this effect fires."""
    def effect(world: WorldState, params: dict) -> None:
        sup = world.suppliers[supplier_id]
        if not sup.released or sup.spare.get(part_id, 0.0) < qty:
            world.log("order_refused", supplier_id=supplier_id, part_id=part_id)
            return
        sup.spare[part_id] -= qty
        world.shipments.append(Shipment(
            shipment_id=f"ORD-{tag}-{supplier_id}-{part_id}", supplier_id=supplier_id, part_id=part_id, qty=qty,
            book_qty=qty, arrives_at=world.now, book_eta=world.now, mode="road"))
    return effect


def _offer_cost(world: WorldState, supplier_id: str, part_id: str, qty: float) -> float:
    o = world.suppliers[supplier_id].offer[part_id]
    return round(world.to_eur(qty * o["unit_price"] * (1 + o["surcharge_pct"] / 100.0), o["currency"]), 2)


def _offer_timing(world: WorldState, supplier_id: str, part_id: str, promised_hours: float) -> dict:
    """Remedy fields of an accepted offer: it lands late by the slip the scenario drew for this supplier."""
    slip = world.truth.get("slips", {}).get(supplier_id, 0.0)
    return {"delay_hours": promised_hours + slip,
            "offer": {"supplier_id": supplier_id, "part_id": part_id, "promised_hours": promised_hours}}


class _Base:
    type: ClassVar[str] = ""
    decoy: ClassVar[bool] = False

    def to_dict(self) -> dict:
        return {"type": self.type, **asdict(self)}  # type: ignore[call-overload]

    @property
    def target(self) -> tuple:
        raise NotImplementedError

    @property
    def id(self) -> str:
        return f"{self.type}:{'|'.join(str(t) for t in self.target)}"

    def _labels(self, target: dict, truth: dict, book: dict, fix: list[str], problem: str) -> dict:
        return {"id": self.id, "type": self.type, "decoy": self.decoy, "target": target, "truth": truth,
                "book": book, "fix": fix, "problem": problem}


# ---------------------------------------------------------------------------
# booking_error: a receipt keyed at a wrong factor, stock is lower than the book
# ---------------------------------------------------------------------------
@dataclass
class BookingError(_Base):
    type: ClassVar[str] = "booking_error"
    part_id: str
    batch: str
    true_qty: float
    supplier_id: str | None = None  # a supplier with spare stock, if any
    offer_qty: float = 0.0
    offer_ready_hours: float = 4.0
    offer_transit_hours: float = 9.0
    release_hours: float = 6.0  # when the corrected delivery note becomes visible

    @property
    def target(self) -> tuple:
        return (self.part_id, self.batch)

    @classmethod
    def sample(cls, world: WorldState, rng: random.Random) -> "BookingError | None":
        claimed = _claimed(world)
        cands = [b for b in world.batches if b.part_id in _bom_parts(world) and b.qty == b.book_qty
                 and b.book_qty >= 1000 and b.quality == "ok" and ("batch", b.part_id, b.batch) not in claimed]
        if not cands:
            return None
        b = rng.choice(cands)
        factor = rng.choice([2, 5, 10, 10])
        true_qty = float(round(b.book_qty / factor))
        gap = b.book_qty - true_qty
        sup = _alt_supplier(world, b.part_id, rng) if rng.random() < 0.85 else None
        offer_qty = max(100.0, float(round(gap * rng.choice([0.3, 0.6, 1.0]) / 100) * 100)) if sup else 0.0
        return cls(b.part_id, b.batch, true_qty, sup, offer_qty, float(rng.randint(2, 6)),
                   float(rng.randint(4, 20)), float(rng.randint(2, 12)))

    def apply(self, world: WorldState, rng: random.Random | None = None) -> None:
        b = world.find_batch(self.part_id, self.batch)
        if b is None:
            raise ValueError(f"no batch {self.part_id}/{self.batch}")
        b.qty = self.true_qty
        _claimed(world).add(("batch", self.part_id, self.batch))
        if self.supplier_id and self.offer_qty > 0:
            _add_offer(world, self.supplier_id, self.part_id, self.offer_qty,
                       self.offer_ready_hours, self.offer_transit_hours)

    def observables(self, world: WorldState) -> list[tuple[datetime, Claim]]:
        b = world.find_batch(self.part_id, self.batch)
        t0, t1 = world.now, world.now + timedelta(hours=self.release_hours)
        out = [
            _obs(world, t0, "receipt", self.batch, "part_id", self.part_id, "erp"),
            _obs(world, t0, "receipt", self.batch, "qty_received", b.book_qty if b else None, "erp", unit="PC"),
            _obs(world, t1, "delivery_note", f"DN-{self.batch}", "part_id", self.part_id, "delivery_note",
                 flags=("corrected",)),
            _obs(world, t1, "delivery_note", f"DN-{self.batch}", "qty", self.true_qty, "delivery_note",
                 flags=("corrected",), unit="PC", quote=f"printed quantity crossed out, {self.true_qty:g} handwritten"),
        ]
        if self.supplier_id and self.offer_qty > 0:
            out += _offer_claim(world, self.supplier_id, self.part_id, t1)
        return out

    def remedies(self, world: WorldState) -> list[Remedy]:
        part, batch = self.part_id, self.batch

        def fix_book(w: WorldState, params: dict) -> None:
            b = w.find_batch(part, batch)
            if b is not None:
                b.book_qty = b.qty

        out = [Remedy(
            id=f"{self.id}:data_fix", disruption_id=self.id, approver_role="site_lead", human_items=1,
            match={"kind": "data_fix_request", "entity": ["stock", "receipt"],
                   "key": [make_key(world.site, part, batch), batch]},
            effect=fix_book)]
        if self.supplier_id and self.offer_qty > 0:
            sid, qty = self.supplier_id, self.offer_qty
            out.append(Remedy(
                id=f"{self.id}:offer", disruption_id=self.id,
                match={"kind": "accept_offer", "supplier_id": sid, "part_id": part},
                effect=_receive_order(sid, part, qty, "offer"),
                cost_eur=_offer_cost(world, sid, part, qty),
                **_offer_timing(world, sid, part, self.offer_ready_hours + self.offer_transit_hours)))
        return out

    def labels(self) -> dict:
        return self._labels(
            {"part_id": self.part_id, "batch": self.batch},
            {"qty": self.true_qty}, {"qty": "book quantity of the batch at kickoff"},
            ["data_fix_request"] + (["accept_offer"] if self.supplier_id else []),
            "A receipt was booked at the wrong quantity; real stock is lower than the book.")


# ---------------------------------------------------------------------------
# rival_offers (hard tier): a shortage with a cheap offer from an unreliable supplier and a dearer one
# from a reliable supplier. The promise of the cheap one looks fine; only the supplier's record says otherwise.
# ---------------------------------------------------------------------------
CHEAP_MAX_RATE, SAFE_MIN_RATE = 0.6, 0.9
GAP_VALUE_EUR = 300_000.0  # missing the gap is worth between one and two times this


def _last_pack_time(world: WorldState, hours_needed: float) -> datetime:
    """The latest moment from which `hours_needed` shift hours still fit before the deadline."""
    t, left = world.deadline, hours_needed
    while left > 0 and t > world.now:
        t -= timedelta(hours=1)
        if world.in_shift(t):
            left -= 1
    return t


@dataclass
class RivalOffers(_Base):
    type: ClassVar[str] = "rival_offers"
    part_id: str
    batch: str
    true_qty: float
    cheap_supplier: str
    safe_supplier: str
    offer_qty: float  # each offer covers the whole gap
    cheap_price: float
    safe_price: float
    currency: str
    cheap_hours: float  # promised hours from the moment the offers appear
    safe_hours: float
    release_hours: float = 6.0

    @property
    def target(self) -> tuple:
        return (self.part_id, self.batch)

    def _book_gap(self) -> BookingError:
        return BookingError(self.part_id, self.batch, self.true_qty, release_hours=self.release_hours)

    @classmethod
    def sample(cls, world: WorldState, rng: random.Random, values: dict[str, float]) -> "RivalOffers | None":
        """A shortage on a part of the most valuable product, sized so that missing the gap costs GAP_VALUE_EUR."""
        prods = sorted((p for p in world.bom if values.get(p, 0.0) > 0), key=lambda p: (-values[p], p))
        if not prods:
            return None
        product = prods[0]
        others = {k for p, parts in world.bom.items() if p != product for k in parts}
        rate = sum(r for pid, r in world.lines.values() if pid == product)
        gap_copies = float(math.ceil(rng.uniform(1.0, 2.0) * GAP_VALUE_EUR / values[product] / 100) * 100)
        release = float(rng.randint(6, 10))
        parts = sorted(k for k in world.bom[product] if k not in others and rate > 0)
        rng.shuffle(parts)
        for part in parts:
            per = world.bom[product][part]
            gap = gap_copies * per
            have = sum(b.qty for b in world.batches if b.part_id == part)
            need = world.remaining(product) * per
            cut = max(gap, have - (need - gap))
            batches = [b for b in world.batches if b.part_id == part and b.qty == b.book_qty and b.quality == "ok"
                       and b.qty >= cut + 500 and ("batch", part, b.batch) not in _claimed(world)]
            suppliers = [s for s in sorted(world.suppliers.values(), key=lambda s: s.supplier_id) if s.released]
            holders = [s for s in suppliers if part in s.parts]
            cheap = [s for s in suppliers if s.on_time_rate <= CHEAP_MAX_RATE]
            safe = [s for s in suppliers if s.on_time_rate >= SAFE_MIN_RATE]
            cheap, safe = [s for s in cheap if s in holders] or cheap, [s for s in safe if s in holders] or safe
            if not batches or not cheap or not safe:
                continue
            b = max(batches, key=lambda x: (x.qty, x.batch))
            cheap_s, safe_s = rng.choice(cheap), rng.choice(safe)
            ref = min(((s.parts[part]["unit_price"], s.parts[part]["currency"]) for s in holders),
                      default=(1.0, "EUR"))
            t_appear = world.now + timedelta(hours=release)
            t_last = _last_pack_time(world, gap_copies / rate)
            cheap_hours = float(round((t_last - timedelta(hours=rng.uniform(3.0, 6.0)) - t_appear)
                                      .total_seconds() / 3600.0))
            safe_hours = float(max(6.0, round((t_last - timedelta(hours=2.0 + safe_s.slip_hours) - t_appear)
                                              .total_seconds() / 3600.0)))
            if cheap_hours < 6.0:
                continue
            return cls(part, b.batch, b.qty - cut, cheap_s.supplier_id, safe_s.supplier_id, gap,
                       ref[0], round(ref[0] * (1.0 + rng.uniform(0.15, 0.40)), 4), ref[1], cheap_hours,
                       min(safe_hours, cheap_hours), release)
        return None

    def apply(self, world: WorldState, rng: random.Random | None = None) -> None:
        self._book_gap().apply(world)
        for sid, price, hours in ((self.cheap_supplier, self.cheap_price, self.cheap_hours),
                                  (self.safe_supplier, self.safe_price, self.safe_hours)):
            _add_offer(world, sid, self.part_id, self.offer_qty, 2.0, hours - 2.0, (price, self.currency))

    def observables(self, world: WorldState) -> list[tuple[datetime, Claim]]:
        out = self._book_gap().observables(world)
        t = world.now + timedelta(hours=self.release_hours)
        for sid in (self.cheap_supplier, self.safe_supplier):
            out += _offer_claim(world, sid, self.part_id, t)
        return out

    def remedies(self, world: WorldState) -> list[Remedy]:
        out = [replace(r, id=r.id.replace("booking_error", self.type, 1), disruption_id=self.id)
               for r in self._book_gap().remedies(world)]
        for tag, sid, hours in (("cheap", self.cheap_supplier, self.cheap_hours),
                                ("safe", self.safe_supplier, self.safe_hours)):
            out.append(Remedy(
                id=f"{self.id}:{tag}", disruption_id=self.id,
                match={"kind": "accept_offer", "supplier_id": sid, "part_id": self.part_id},
                effect=_receive_order(sid, self.part_id, self.offer_qty, tag),
                cost_eur=_offer_cost(world, sid, self.part_id, self.offer_qty),
                **_offer_timing(world, sid, self.part_id, hours)))
        return out

    def labels(self) -> dict:
        return self._labels(
            {"part_id": self.part_id, "batch": self.batch}, {"qty": self.true_qty},
            {"qty": "book quantity of the batch at kickoff"}, ["data_fix_request", "accept_offer"],
            "A shortage with two offers: a cheap one from an unreliable supplier and a dearer one from a reliable one.")


# ---------------------------------------------------------------------------
# short_shipment: an inbound shipment carries less than booked
# ---------------------------------------------------------------------------
@dataclass
class ShortShipment(_Base):
    type: ClassVar[str] = "short_shipment"
    shipment_id: str
    shipped_qty: float
    balance_arrives_at: str | None = None  # ISO; None means two days after the deadline
    release_hours: float = 6.0  # when the supplier's shipped-quantity claim appears
    on_request: bool = False  # the claim only appears when someone asks
    alt_supplier_id: str | None = None  # second source with spare stock
    alt_qty: float = 0.0
    alt_ready_hours: float = 4.0
    alt_transit_hours: float = 9.0
    freight_hours: float | None = None  # hours to fly the balance, if that is possible at all
    freight_eur_per_unit: float = 1.2

    @property
    def target(self) -> tuple:
        return (self.shipment_id,)

    @classmethod
    def sample(cls, world: WorldState, rng: random.Random) -> "ShortShipment | None":
        s = _shipment_targets(world, rng, "any")
        if s is None:
            return None
        shipped = float(round(s.book_qty * rng.choice([0.4, 0.6, 0.7, 0.8]) / 100) * 100)
        balance = s.book_qty - shipped
        alt = _alt_supplier(world, s.part_id, rng, exclude=s.supplier_id) if rng.random() < 0.5 else None
        return cls(
            s.shipment_id, shipped,
            (world.deadline + timedelta(hours=rng.randint(24, 72))).isoformat(),
            float(rng.randint(2, 12)), rng.random() < 0.3,
            alt, float(round(balance * rng.choice([0.5, 1.0]) / 100) * 100) if alt else 0.0,
            float(rng.randint(2, 6)), float(rng.randint(4, 20)),
            float(rng.choice([30, 36, 48])) if rng.random() < 0.4 else None)

    def apply(self, world: WorldState, rng: random.Random | None = None) -> None:
        s = world.find_shipment(self.shipment_id)
        if s is None:
            raise ValueError(f"no shipment {self.shipment_id}")
        balance = s.book_qty - self.shipped_qty
        s.qty = self.shipped_qty
        _claimed(world).add(("shipment", self.shipment_id))
        if balance > 0:
            late = (datetime.fromisoformat(self.balance_arrives_at) if self.balance_arrives_at
                    else world.deadline + 2 * DAY)
            world.shipments.append(Shipment(
                shipment_id=f"{self.shipment_id}-balance", supplier_id=s.supplier_id, part_id=s.part_id,
                qty=balance, book_qty=0.0, arrives_at=late, book_eta=None, mode=s.mode, reference=s.reference,
                origin=s.origin, destination=s.destination, book_status="not_in_book"))
        if self.alt_supplier_id and self.alt_qty > 0:
            _add_offer(world, self.alt_supplier_id, s.part_id, self.alt_qty,
                       self.alt_ready_hours, self.alt_transit_hours)

    def observables(self, world: WorldState) -> list[tuple[datetime, Claim]]:
        s = world.find_shipment(self.shipment_id)
        release = world.deadline if self.on_request else world.now + timedelta(hours=self.release_hours)
        flags = ("on_request",) if self.on_request else ()
        out = [
            _obs(world, release, "shipment", self.shipment_id, "qty_shipped", self.shipped_qty,
                 f"shipping_advice:{s.supplier_id}", flags=flags, unit="PC",
                 quote=f"only {self.shipped_qty:g} of {s.book_qty:g} shipped; the balance follows later"),
            _obs(world, release, "shipment", self.shipment_id, "note",
                 "partial shipment; balance follows after the deadline", f"shipping_advice:{s.supplier_id}",
                 flags=flags),
        ]
        if self.alt_supplier_id and self.alt_qty > 0:
            out += _offer_claim(world, self.alt_supplier_id, s.part_id, world.now + timedelta(hours=self.release_hours))
        return out

    def remedies(self, world: WorldState) -> list[Remedy]:
        s = world.find_shipment(self.shipment_id)
        part = s.part_id
        out: list[Remedy] = []
        sharing = [p for p, parts in world.bom.items() if part in parts]
        if len(sharing) >= 2:
            def reallocate(w: WorldState, params: dict) -> None:
                order = [p for p in params.get("order", []) if p in w.products]
                if not order:  # highest value per copy first
                    value = w.tenant.get("value_per_copy", {})
                    order = sorted(w.priority, key=lambda p: -float(value.get(p, 0.0)))
                w.priority = order + [p for p in w.priority if p not in order]
                w.reserve = True  # hold back what the first product still needs

            out.append(Remedy(id=f"{self.id}:reallocate", disruption_id=self.id,
                              match={"kind": "set_priority"}, effect=reallocate))
        if self.alt_supplier_id and self.alt_qty > 0:
            sid, qty = self.alt_supplier_id, self.alt_qty
            out.append(Remedy(
                id=f"{self.id}:second_source", disruption_id=self.id,
                match={"kind": "accept_offer", "supplier_id": sid, "part_id": part},
                effect=_receive_order(sid, part, qty, "second"), cost_eur=_offer_cost(world, sid, part, qty),
                **_offer_timing(world, sid, part, self.alt_ready_hours + self.alt_transit_hours)))
        if self.freight_hours is not None:
            balance = s.book_qty - self.shipped_qty
            out.append(Remedy(
                id=f"{self.id}:freight", disruption_id=self.id,
                match={"kind": "book_freight", "part_id": part},
                effect=_pull_balance_forward(self.shipment_id),
                cost_eur=round(balance * self.freight_eur_per_unit, 2), delay_hours=self.freight_hours))
        return out

    def labels(self) -> dict:
        return self._labels(
            {"shipment_id": self.shipment_id}, {"qty": self.shipped_qty},
            {"qty": "quantity booked in the ERP shipment"},
            ["set_priority"] + (["accept_offer"] if self.alt_supplier_id else [])
            + (["book_freight"] if self.freight_hours is not None else []),
            "An inbound shipment carries less than booked.")


def _pull_balance_forward(shipment_id: str) -> Callable[[WorldState, dict], None]:
    """Effect: freight brings the balance of a short shipment earlier than its normal late date."""
    def effect(world: WorldState, params: dict) -> None:
        late = world.find_shipment(f"{shipment_id}-balance")
        if late is not None and not late.delivered:
            late.arrives_at = min(late.arrives_at, world.now) if late.arrives_at else world.now
    return effect


# ---------------------------------------------------------------------------
# failed_lot: a batch failed inspection but is booked unrestricted
# ---------------------------------------------------------------------------
@dataclass
class FailedLot(_Base):
    type: ClassVar[str] = "failed_lot"
    part_id: str
    batch: str
    defect_rate: float = 0.07
    release_hours: float = 6.0  # when the inspection report becomes visible

    @property
    def target(self) -> tuple:
        return (self.part_id, self.batch)

    @classmethod
    def sample(cls, world: WorldState, rng: random.Random) -> "FailedLot | None":
        claimed = _claimed(world)
        cands = [b for b in world.batches if b.part_id in _bom_parts(world) and b.quality == "ok"
                 and b.qty >= 500 and ("batch", b.part_id, b.batch) not in claimed]
        if not cands:
            return None
        b = rng.choice(cands)
        return cls(b.part_id, b.batch, rng.choice([0.05, 0.07, 0.09, 0.12]), float(rng.randint(2, 12)))

    def apply(self, world: WorldState, rng: random.Random | None = None) -> None:
        b = world.find_batch(self.part_id, self.batch)
        if b is None:
            raise ValueError(f"no batch {self.part_id}/{self.batch}")
        b.quality = "failed"  # the book stays unrestricted
        _claimed(world).add(("batch", self.part_id, self.batch))

    def _approver(self, world: WorldState) -> str:
        return world.parts.get(self.part_id, {}).get("category_lead", "head_of_procurement")

    def observables(self, world: WorldState) -> list[tuple[datetime, Claim]]:
        b = world.find_batch(self.part_id, self.batch)
        t = world.now + timedelta(hours=self.release_hours)
        ev = f"inspection:{self.batch}"
        return [
            _obs(world, t, "inspection", self.batch, "part_id", self.part_id, ev),
            _obs(world, t, "inspection", self.batch, "lot_qty", b.book_qty, ev, unit="PC"),
            _obs(world, t, "inspection", self.batch, "result", "failed", ev,
                 quote=f"AQL not met, defect rate {self.defect_rate:.1%}"),
            _obs(world, t, "inspection", self.batch, "defect_rate", self.defect_rate, ev),
            _obs(world, t, "inspection", self.batch, "booked_status", b.book_quality, ev),
            _obs(world, t, "inspection", self.batch, "required_signoff",
                 f"supplier_quality_lead,{self._approver(world)}", ev),
        ]

    def remedies(self, world: WorldState) -> list[Remedy]:
        part, batch = self.part_id, self.batch

        def quarantine(w: WorldState, params: dict) -> None:
            b = w.find_batch(part, batch)
            if b is not None:
                b.quarantined = True

        def concede(w: WorldState, params: dict) -> None:
            b = w.find_batch(part, batch)
            if b is not None:
                b.concession = True
                b.quarantined = False  # accepting the lot lifts the hold the team put on it

        return [
            Remedy(id=f"{self.id}:quarantine", disruption_id=self.id,
                   match={"kind": "quarantine_batch", "part_id": part, "batch": batch}, effect=quarantine),
            Remedy(id=f"{self.id}:concession", disruption_id=self.id, approver_role=self._approver(world),
                   human_items=1, match={"kind": "request_concession", "part_id": part, "gr_number": batch},
                   effect=concede),
        ]

    def labels(self) -> dict:
        return self._labels(
            {"part_id": self.part_id, "batch": self.batch},
            {"quality": "failed", "defect_rate": self.defect_rate}, {"quality": "unrestricted"},
            ["quarantine_batch", "request_concession"],
            "A batch failed inspection but is booked as usable.")


# ---------------------------------------------------------------------------
# stuck_at_port: arrived, but needs pick-up and a dock slot
# ---------------------------------------------------------------------------
def _clear_need(shipment_id: str, need: str) -> Callable[[WorldState, dict], None]:
    def effect(world: WorldState, params: dict) -> None:
        s = world.find_shipment(shipment_id)
        if s is None:
            return
        s.needs.discard(need)
        if not s.needs and s.arrives_at is None:  # both granted: the last leg starts now
            s.arrives_at = world.now + timedelta(hours=s.road_hours)
    return effect


@dataclass
class StuckAtPort(_Base):
    type: ClassVar[str] = "stuck_at_port"
    shipment_id: str

    @property
    def target(self) -> tuple:
        return (self.shipment_id,)

    @classmethod
    def sample(cls, world: WorldState, rng: random.Random) -> "StuckAtPort | None":
        s = _shipment_targets(world, rng, "port")
        return cls(s.shipment_id) if s else None

    def apply(self, world: WorldState, rng: random.Random | None = None) -> None:
        s = world.find_shipment(self.shipment_id)
        if s is None:
            raise ValueError(f"no shipment {self.shipment_id}")
        s.needs |= {"pickup", "dock_slot"}
        s.arrives_at = None
        s.book_status = "arrived_port"
        s.road_hours = s.road_hours or 24.0
        if s.book_eta is None or s.book_eta > world.now:
            s.book_eta = world.now - timedelta(hours=12)  # the book says it already arrived
        _claimed(world).add(("shipment", self.shipment_id))

    def observables(self, world: WorldState) -> list[tuple[datetime, Claim]]:
        t = world.now
        return [
            _obs(world, t, "shipment", self.shipment_id, "status", "in_transit", "tracker",
                 quote="in transit, call terminal"),
            _obs(world, t, "shipment", self.shipment_id, "note", "discharged at the port, available for pick-up",
                 "erp"),
        ]

    def remedies(self, world: WorldState) -> list[Remedy]:
        sid = self.shipment_id
        ids = (f"{self.id}:pickup", f"{self.id}:dock_slot")
        return [
            Remedy(id=ids[0], disruption_id=self.id, approver_role="site_lead", human_items=1, peers=(ids[1],),
                   match={"kind": "request_pickup", "shipment_id": sid}, effect=_clear_need(sid, "pickup")),
            Remedy(id=ids[1], disruption_id=self.id, approver_role="site_lead", human_items=1, peers=(ids[0],),
                   match={"kind": "request_dock_slot", "shipment_id": sid}, effect=_clear_need(sid, "dock_slot")),
        ]

    def labels(self) -> dict:
        return self._labels(
            {"shipment_id": self.shipment_id}, {"status": "arrived_port, needs pick-up and dock slot"},
            {"status": "arrived_port"}, ["request_pickup", "request_dock_slot"],
            "A shipment sits at the port and needs a pick-up and a dock slot; the tracker says in transit.")


# ---------------------------------------------------------------------------
# terms_hold: a supplier holds a shipment until delivery terms are confirmed
# ---------------------------------------------------------------------------
@dataclass
class TermsHold(_Base):
    type: ClassVar[str] = "terms_hold"
    shipment_id: str
    supplier_id: str
    po_number: str
    po_term: str = "DDP"
    contract_term: str = "EXW"
    release_hours: float = 6.0

    @property
    def target(self) -> tuple:
        return (self.shipment_id,)

    @classmethod
    def sample(cls, world: WorldState, rng: random.Random) -> "TermsHold | None":
        s = _shipment_targets(world, rng, "any")
        if s is None:
            return None
        po = s.po_numbers[0] if s.po_numbers else f"PO-{s.shipment_id}"
        po_term, contract_term = rng.choice([("DDP", "EXW"), ("DAP", "FCA"), ("DDP", "FOB")])
        return cls(s.shipment_id, s.supplier_id, po, po_term, contract_term, float(rng.randint(2, 12)))

    def apply(self, world: WorldState, rng: random.Random | None = None) -> None:
        s = world.find_shipment(self.shipment_id)
        if s is None:
            raise ValueError(f"no shipment {self.shipment_id}")
        s.needs.add("terms")
        s.arrives_at = None
        if self.po_number not in s.po_numbers:
            s.po_numbers.append(self.po_number)  # the held order is this shipment's order
        _claimed(world).add(("shipment", self.shipment_id))

    def observables(self, world: WorldState) -> list[tuple[datetime, Claim]]:
        s = world.find_shipment(self.shipment_id)
        t = world.now + timedelta(hours=self.release_hours)
        reason = (f"Shipment held until delivery terms are confirmed: the PO says {self.po_term}, "
                  f"the agreement says {self.contract_term}.")
        oc = make_key(self.po_number, s.part_id, f"OA-{self.shipment_id}")
        return [
            _obs(world, t, "order_confirmation", oc, "supplier_id", self.supplier_id, "order_confirmation"),
            _obs(world, t, "order_confirmation", oc, "incoterm", self.contract_term, "order_confirmation"),
            _obs(world, t, "order_confirmation", oc, "hold_reason", reason, "order_confirmation", quote=reason),
            _obs(world, world.now, "po_line", make_key(self.po_number, 1), "incoterm", self.po_term, "erp"),
            _obs(world, t, "contract_term", make_key(self.supplier_id, "incoterm"), "value", self.contract_term,
                 "contract"),
        ]

    def remedies(self, world: WorldState) -> list[Remedy]:
        sid = self.shipment_id
        sup = world.suppliers[self.supplier_id]

        def release(w: WorldState, params: dict) -> None:
            s = w.find_shipment(sid)
            if s is None:
                return
            s.needs.discard("terms")
            if not s.needs and s.arrives_at is None:
                s.arrives_at = max(s.book_eta, w.now) if s.book_eta else w.now

        return [Remedy(id=f"{self.id}:confirm", disruption_id=self.id, emails=1, delay_hours=sup.response_hours,
                       match={"kind": "confirm_terms", "supplier_id": self.supplier_id,
                              "po_number": self.po_number}, effect=release)]

    def labels(self) -> dict:
        return self._labels(
            {"shipment_id": self.shipment_id, "supplier_id": self.supplier_id, "po_number": self.po_number},
            {"hold": "terms"}, {"incoterm": self.po_term}, ["confirm_terms"],
            "A supplier holds a shipment until the delivery terms are confirmed in writing.")


# ---------------------------------------------------------------------------
# decoy: nothing is wrong
# ---------------------------------------------------------------------------
@dataclass
class Decoy(_Base):
    type: ClassVar[str] = "decoy"
    decoy: ClassVar[bool] = True
    kind: str  # "fitted_part" or "plan_flag"
    part_id: str
    release_hours: float = 0.0

    @property
    def target(self) -> tuple:
        return (self.kind, self.part_id)

    @classmethod
    def sample(cls, world: WorldState, rng: random.Random) -> "Decoy | None":
        claimed = _claimed(world)
        options: list[tuple[str, str]] = [("fitted_part", p) for p in sorted(world.fitted_into)
                                          if ("decoy", p) not in claimed]
        have = {}
        for b in world.batches:
            have[b.part_id] = have.get(b.part_id, 0.0) + b.qty
        options += [("plan_flag", p) for p in sorted(_bom_parts(world))
                    if have.get(p, 0.0) > 0 and ("decoy", p) not in claimed]
        if not options:
            return None
        kind, part = rng.choice(options)
        return cls(kind, part)

    def apply(self, world: WorldState, rng: random.Random | None = None) -> None:
        _claimed(world).add(("decoy", self.part_id))

    def observables(self, world: WorldState) -> list[tuple[datetime, Claim]]:
        t = world.now + timedelta(hours=self.release_hours)
        out = [_obs(world, t, "observation", make_key("plan_flag", self.part_id), "text",
                    f"Plan flags {self.part_id}: CHECK, cover shows negative.", "plan")]
        if self.kind == "fitted_part":
            out.append(_obs(world, t, "part", self.part_id, "supplied_fitted_into",
                            world.fitted_into.get(self.part_id), "supplier_components",
                            quote="fitted by supplier; not ordered, received or stocked separately"))
        return out

    def remedies(self, world: WorldState) -> list[Remedy]:
        return []

    def labels(self) -> dict:
        return self._labels({"part_id": self.part_id, "kind": self.kind}, {"problem": None},
                            {"flag": "CHECK"}, [], "False alarm: nothing is wrong.")


DISRUPTIONS: dict[str, type] = {
    cls.type: cls for cls in (BookingError, ShortShipment, FailedLot, StuckAtPort, TermsHold, Decoy, RivalOffers)
}

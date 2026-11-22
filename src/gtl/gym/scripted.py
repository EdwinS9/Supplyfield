"""Rule-based counterparts for the Gym: supplier replies, staff answers and the claims a mail reader would read.

These are the default until LLM personas exist. A reply states exactly the
facts in the fact sheet and nothing else, so the world stays the only source
of truth.
"""
from __future__ import annotations

import hashlib

from gtl.contracts import (Action, Claim, Context, Evidence, HumanAnswer, InboundMail, OutboundMail, Post,
                           make_key)
from gtl.gym.world import WorldState, fact_sheet

OFFER_FIELDS = ("qty", "unit_price", "currency", "surcharge_pct", "ready_hours", "transit_hours", "valid_until")
LIAISON = "supply_liaison"
HOLD_NOTE = "held by the supplier until the delivery terms are confirmed"


def _live_shipments(sheet: dict) -> list[dict]:
    """Shipments still on their way that were ordered through the book (balances are not)."""
    return [s for s in sheet["shipments"] if not s["delivered"] and s["qty_ordered"] > 0]


def _live_offers(sheet: dict) -> dict[str, dict]:
    return {p: o for p, o in sheet["offers"].items() if sheet["spare_stock"].get(p, 0.0) >= o["qty"] > 0}


def scripted_reply(fact_sheet: dict, traits: dict, mail: OutboundMail) -> str:
    lines: list[str] = []
    for s in _live_shipments(fact_sheet):
        line = f"Shipment {s['shipment_id']} ({s['part_id']}): we shipped {s['qty_shipped']:g} of {s['qty_ordered']:g} ordered."
        if s["holds"]:
            line += " It is on hold until the delivery terms are confirmed."
        elif s["arrives_at"]:
            line += f" Expected arrival {s['arrives_at']}."
        else:
            line += " There is no arrival date yet."
        lines.append(line)
    for part, o in _live_offers(fact_sheet).items():
        lines.append(f"We have {fact_sheet['spare_stock'][part]:g} of {part} in finished stock. "
                     f"We can ship {o['qty']:g} at {o['unit_price']:g} {o['currency']} each, ready in "
                     f"{o['ready_hours']:g} hours with {o['transit_hours']:g} hours of transit.")
    if not lines:
        lines.append("We have nothing to report on your orders and no spare stock.")
    return "\n".join(lines)


def scripted_answer(person, post: Post) -> HumanAnswer:  # type: ignore[no-untyped-def]
    """Approve what pays for itself or costs nothing; otherwise take the last option, "Do not do this"."""
    if post.kind == "defect":
        return HumanAnswer(post_id=post.id, answered_by=person.role_id, approved=True, text="Corrected in the system.")
    options = post.body["options"]
    best = options[post.body.get("recommended", 0)]
    if best["net_value_eur"] > 0 or best["cost_eur"] <= 0:
        return HumanAnswer(post_id=post.id, answered_by=person.role_id, choice=post.body.get("recommended", 0),
                           approved=True, text="Approved.")
    return HumanAnswer(post_id=post.id, answered_by=person.role_id, choice=len(options) - 1,
                       approved=False, text="Not worth the cost.")


def reply_claims(world: WorldState, supplier_id: str, mail: InboundMail, now_iso: str) -> list[Claim]:
    """The facts of a scripted reply as claims in the canonical vocabulary."""
    sheet = fact_sheet(world, supplier_id)
    source = f"mail:{mail.message_id}"
    out: list[Claim] = []

    def add(entity: str, key: str, fld: str, value, quote: str, unit: str | None = None) -> None:  # type: ignore[no-untyped-def]
        out.append(Claim(entity=entity, key=key, field=fld, value=value, as_of=now_iso, source_kind="mail",
                         evidence=(Evidence(source=source, quote=quote),), author="mail_reader", unit=unit))

    for s in _live_shipments(sheet):
        sid = s["shipment_id"]
        quote = f"we shipped {s['qty_shipped']:g} of {s['qty_ordered']:g} ordered"
        add("shipment", sid, "qty_shipped", s["qty_shipped"], quote, "PC")
        if s["holds"]:
            add("shipment", sid, "note", HOLD_NOTE, "on hold until the delivery terms are confirmed")
        elif s["arrives_at"]:
            add("shipment", sid, "eta_site", s["arrives_at"], f"expected arrival {s['arrives_at']}")
    for part, o in _live_offers(sheet).items():
        key = make_key(supplier_id, f"OFFER-{part}", 1)
        quote = f"we can ship {o['qty']:g} of {part}"
        add("offer", key, "part_id", part, quote)
        for f in OFFER_FIELDS:
            add("offer", key, f, o[f], quote)
    return out


class ScriptedLiaison:
    """Stands in for the supply liaison in the Gym: a question to a supplier becomes a plain email.

    The mail goes from the supply inbox (the quality inbox for quality questions) to the supplier's
    contact address on the Board. The question is marked asked. Questions to anyone else stay open.
    """

    name = LIAISON
    subscribes = ("question:open",)

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        q = post.body
        if q.get("to_kind") != "supplier" or ctx.board.get(post.id).status != "open":
            return []
        to = next((str(f.value) for f in ctx.board.facts("supplier", key_prefix=q["to_id"])
                   if f.key == q["to_id"] and f.field == "contact_email" and f.value), None)
        if to is None:
            return []
        channel = "quality" if q.get("channel") == "quality" else "supply"
        mates = [p for p in ctx.board.query(kind="question", status="open", limit=100_000)
                 if p.body.get("to_kind") == "supplier" and p.body.get("to_id") == q["to_id"]
                 and ("quality" if p.body.get("channel") == "quality" else "supply") == channel]
        mates.sort(key=lambda p: -int(p.body.get("copies_at_risk") or 0))
        ids = [p.id for p in mates]
        body = mates[0].body["text"] if len(mates) == 1 else \
            "\n".join(f"{n}. {p.body['text']}" for n, p in enumerate(mates, 1))
        digest = hashlib.sha1("|".join(sorted(ids)).encode()).hexdigest()[:8]
        key = f"send_email:{q['to_id']}|{channel}|{digest}"
        action = Action("send_email", {"to": to, "subject": f"Question: {mates[0].body['topic']}", "body": body,
                                       "body_en": body, "inbox": ctx.tenant["inboxes"][channel],
                                       "supplier_id": q["to_id"], "question_ids": ids},
                        self.name, f"Question to {q['to_id']} about {mates[0].body['topic']}", key,
                        context_post_ids=tuple(ids))
        for qid in ids:
            ctx.board.set_status(qid, "asked", self.name)
        return [Post(kind="action", status="requested", subject=key, body=action.to_dict(), author=self.name,
                     depends_on=tuple(ids))]

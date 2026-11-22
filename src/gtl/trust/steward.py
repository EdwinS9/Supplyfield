"""The data steward: corrects the record, finds the owner, raises a defect."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from gtl.contracts import Action, Board, Context, Defect, Fact, Post, Question
from gtl.plan import build_plan_input, plan, what_if
from gtl.trust.common import (fact_table, find_fact_post, pick_preferred, slot_subject, to_number, values_match)

NAME = "data_steward"
CORRECTING = ("cross_source", "receipt_vs_note", "receipt_vs_order", "shipment_vs_advice")
SUPPLIER_QUESTION_CHECKS = ("revision_match", "terms_match", "shipment_vs_advice", "stale_feed")
QUALITY_CHECKS = ("revision_match", "inspection_vs_stock", "revision_not_released", "revision_unknown")
SUPPLIER_OWNED = ("order_confirmation", "offer", "notice", "delivery_note", "supplier_part")
SUPPLIER_KEYED = ("offer", "notice", "supplier_part", "contract_term", "supplier")
DEFECT_CLASSES = ("format", "content", "missing", "stale")

SYSTEM = (
    "You are the data steward of a supply chain control system. A conflict between two records "
    "was found and the record has been corrected on the Board. Classify the problem and decide "
    "whether the record owner must be told. Classes: format (wrong shape or unit), content (a "
    "wrong value), missing (a value is absent), stale (a feed stopped updating). Raise to the "
    "owner when copies are at risk or the owner must fix the source. Answer with JSON only."
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "defect_class": {"type": "string", "enum": list(DEFECT_CLASSES)},
        "raise_to_owner": {"type": "boolean"},
        "detail": {"type": "string"},
    },
    "required": ["defect_class", "raise_to_owner", "detail"],
}


@dataclass
class Correction:
    old: Post | None  # fact post to quarantine
    value: Any  # corrected value; None means quarantine only
    entity: str
    key: str
    field: str
    as_of: str
    claim_ids: tuple[str, ...]
    unit: str | None
    replace: bool = True
    unflag: bool = False  # only drop the provisional flag from the old fact


def _claims(board: Board, ids: list[str]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for cid in ids:
        p = board.get(cid)
        if p is not None:
            out.append((cid, p.body))
    return out


def plan_corrections(board: Board, check: str, entity: str, key: str, field: str,
                     pref_id: str | None, pref: dict[str, Any] | None) -> list[Correction]:
    """What to quarantine and what to write instead. Reads the Board, changes nothing."""
    if check == "inspection_vs_stock":
        old = find_fact_post(board, "stock", key, "qty", ("active",))
        if old is None:
            return []
        return [Correction(old, None, "stock", key, "qty", old.body.get("as_of", ""), (), old.body.get("unit"),
                           replace=False)]
    if check not in CORRECTING or pref is None or pref_id is None:
        return []
    old = find_fact_post(board, entity, key, field, ("active",))
    if old is not None:
        # A provisional fix (made on the order alone) yields to the delivery note.
        yields = check == "receipt_vs_note" and "provisional" in old.body.get("checks_passed", [])
        if values_match(old.body.get("value"), pref["value"]):
            return [Correction(old, None, entity, key, field, "", (), None, replace=False, unflag=True)] \
                if yields else []
        if any(str(c).startswith("steward:") for c in old.body.get("checks_passed", [])) \
                and check != "cross_source" and not yields:
            return []
    out = [Correction(old, pref["value"], entity, key, field, pref["as_of"], (pref_id,), pref.get("unit"))]
    if (entity, field) == ("receipt", "qty_received") and old is not None:
        booked, fixed = to_number(old.body.get("value")), to_number(pref["value"])
        for sp in board.query(kind="fact", status="active", subject_prefix="stock/", limit=100_000):
            sb = sp.body
            if sb.get("field") != "qty" or str(sb.get("key", "")).rsplit("|", 1)[-1] != key:
                continue
            book = to_number(sb.get("value"))
            if book is None or booked is None or fixed is None:
                continue
            new_qty = max(0.0, book - (booked - fixed))
            out.append(Correction(sp, new_qty, "stock", sb["key"], "qty", pref["as_of"],
                                  (*sb.get("claim_ids", []), pref_id), sb.get("unit")))
    return out


def stock_deltas(corrections: list[Correction]) -> dict[str, float]:
    """Units of each part that stop being usable because of the corrections."""
    out: dict[str, float] = {}
    for c in corrections:
        if (c.entity, c.field) != ("stock", "qty") or c.old is None:
            continue
        before = to_number(c.old.body.get("value")) or 0.0
        after = 0.0 if c.value is None else (to_number(c.value) or 0.0)
        parts = c.key.split("|")
        if len(parts) > 1 and before > after:
            out[parts[1]] = out.get(parts[1], 0.0) + (before - after)
    return out


def copies_at_risk(ctx: Context, deltas: dict[str, float]) -> int:
    """Drop in total copies if those units are not usable. 0 when the planner cannot run."""
    if not deltas:
        return 0
    try:
        pi = build_plan_input(ctx.board, ctx.tenant, ctx.ports.clock.now())
        drop = sum(plan(pi).copies.values()) - sum(what_if(pi, remove_stock=deltas).copies.values())
        return max(0, int(round(drop)))
    except Exception:  # noqa: BLE001 - a planner problem must not block the correction
        return 0


def supplier_of(board: Board, entity: str, key: str) -> str | None:
    for f in board.facts(entity=entity):
        if f.key == key and f.field == "supplier_id" and f.value:
            return str(f.value)
    if entity in SUPPLIER_KEYED:
        return key.split("|")[0]
    return None


def owner_role_of(board: Board, entity: str) -> str | None:
    for f in board.facts(entity="record_owner"):
        if f.key == entity and f.field == "role_id" and f.value:
            return str(f.value)
    return None


def fallback_assessment(check: str, conflict: dict[str, Any], preferred: Any, copies: int) -> dict[str, Any]:
    return {"defect_class": "stale" if check == "stale_feed" else "content",
            "raise_to_owner": copies > 0,
            "detail": f"{conflict.get('detail', '')} Preferred value: {preferred}."}


def assess(ctx: Context, conflict: dict[str, Any], claims: list[tuple[str, dict[str, Any]]],
           preferred: Any, copies: int) -> dict[str, Any]:
    """One LLM call; any failure or bad answer falls back to rules."""
    check = conflict.get("check", "")
    fallback = fallback_assessment(check, conflict, preferred, copies)
    system = SYSTEM
    playbook = ctx.playbooks.get("data_steward", "")
    if playbook:
        system = f"{system}\n\n{playbook}"
    user = json.dumps({
        "conflict": conflict,
        "claims": [{"id": cid, "entity": b.get("entity"), "key": b.get("key"), "field": b.get("field"),
                    "value": b.get("value"), "source_kind": b.get("source_kind"), "as_of": b.get("as_of"),
                    "quotes": [e.get("quote") for e in b.get("evidence", []) if e.get("quote")]}
                   for cid, b in claims],
        "preferred_value": preferred,
        "copies_at_risk": copies,
    }, default=str)
    try:
        data = ctx.llm.complete("agent", system, user, schema=SCHEMA).data
    except Exception:  # noqa: BLE001
        return fallback
    if not isinstance(data, dict) or data.get("defect_class") not in DEFECT_CLASSES \
            or not isinstance(data.get("raise_to_owner"), bool) \
            or not isinstance(data.get("detail"), str) or not data["detail"].strip():
        return fallback
    return {"defect_class": data["defect_class"], "raise_to_owner": data["raise_to_owner"],
            "detail": data["detail"].strip()}


def steward_conflict(post: Post, ctx: Context) -> list[Post]:
    board: Any = ctx.board
    c = post.body
    check, entity, key, field = c["check"], c["entity"], c["key"], c["field"]
    claims = _claims(board, list(c.get("claim_ids", [])))
    pref_id = c.get("preferred_claim_id") or pick_preferred(claims)
    pref = dict(claims).get(pref_id) if pref_id else None
    wrong = next(((i, b) for i, b in claims if i != pref_id), None)

    # Copies at risk are priced on the facts as they are before the correction.
    # A sum of several bookings cannot say which booking is wrong: the owner is asked instead.
    corrections = [] if c.get("group") else plan_corrections(board, check, entity, key, field, pref_id, pref)
    already_fixed = (check in CORRECTING and all(c.unflag for c in corrections)
                     and _steward_fixed(board, entity, key, field))
    provisional = check == "receipt_vs_order" and pref is not None and not _note_agrees(board, key, pref["value"])
    copies = copies_at_risk(ctx, stock_deltas(corrections))

    out: list[Post] = []
    for corr in corrections:
        if corr.unflag and corr.old is not None:
            kept = [x for x in corr.old.body.get("checks_passed", []) if x != "provisional"]
            board.update_body(corr.old.id, {**corr.old.body, "checks_passed": kept}, NAME)
            continue
        if corr.old is not None:
            board.update_body(corr.old.id, {**corr.old.body, "quarantined": True}, NAME)
            board.set_status(corr.old.id, "quarantined", NAME, f"{check}: {post.id}")
        if corr.replace:
            fact = Fact(corr.entity, corr.key, corr.field, corr.value, corr.as_of, corr.claim_ids,
                        (f"steward:{check}", *(("provisional",) if provisional and corr.entity == entity else ())),
                        corr.unit)
            deps = tuple(dict.fromkeys((post.id, *([pref_id] if pref_id else []))))
            out.append(Post(kind="fact", status="active", subject=slot_subject(corr.entity, corr.key, corr.field),
                            body=fact.to_dict(), author=NAME, depends_on=deps,
                            supersedes=corr.old.id if corr.old else None))
    if check == "inspection_vs_stock" and any(c.old is not None for c in corrections):
        out.append(_quarantine_action(post, key, c.get("detail", "")))
    if check == "revision_not_released":
        return _hold_unreleased_batch(post, ctx)
    if check == "revision_unknown":
        return _ask_batch_revision(post, ctx)
    if already_fixed:
        board.set_status(post.id, "resolved", NAME, "already corrected")
        return out

    preferred_value = pref["value"] if pref else None
    a = assess(ctx, c, claims, preferred_value, copies)
    if provisional:
        a["raise_to_owner"] = True
    owner_supplier, owner_role = None, None
    if wrong is not None:
        w = wrong[1]
        if w.get("entity") in SUPPLIER_OWNED or w.get("source_kind") == "mail":
            owner_supplier = supplier_of(board, w["entity"], w["key"])
        else:
            owner_role = owner_role_of(board, w["entity"])
    defect = Defect(a["defect_class"], entity, key, a["detail"], post.id, owner_role, owner_supplier,
                    preferred_value if check in CORRECTING and not c.get("group") else None, copies)
    out.append(Post(kind="defect", status="open" if a["raise_to_owner"] else "dismissed", subject=post.subject,
                    body=defect.to_dict(), author=NAME, depends_on=(post.id,)))

    if check == "bank_change":
        role = ctx.tenant.get("bank_change_role", "buyer")
        out.append(_question(a["detail"], "role", role, check, entity, key, "supply", copies, post.id))
    elif owner_supplier is not None or check in SUPPLIER_QUESTION_CHECKS or provisional:
        supplier = owner_supplier or next(
            (s for _, b in claims if (s := supplier_of(board, b["entity"], b["key"]))), None)
        if supplier is not None:
            channel = "quality" if check in QUALITY_CHECKS else "supply"
            text = (f"Receipt {key} books more than was ordered. What quantity did you ship? {a['detail']}"
                    if provisional else a["detail"])
            out.append(_question(text, "supplier", supplier, check, entity, key, channel, copies, post.id))
    board.set_status(post.id, "resolved", NAME)
    return out


def _hold_unreleased_batch(post: Post, ctx: Context) -> list[Post]:
    """Quarantine a batch whose revision is not released, tell the warehouse, ask who used it."""
    board: Any = ctx.board
    c = post.body
    key, detail = c["key"], c.get("detail", "")
    _, part_id, batch = (key.split("|") + ["", ""])[:3]
    out: list[Post] = []
    old = find_fact_post(board, "stock", key, "qty", ("active",))
    copies = 0
    if old is not None:
        copies = copies_at_risk(ctx, {part_id: to_number(old.body.get("value")) or 0.0})
        board.update_body(old.id, {**old.body, "quarantined": True}, NAME)
        board.set_status(old.id, "quarantined", NAME, f"{c['check']}: {post.id}")
    out.append(_quarantine_action(post, key, detail, "Revision not released for"))
    role = owner_role_of(board, "stock")
    if role is not None:
        text = (f"{detail} Which batches of {part_id} were used since the stock snapshot? "
                f"Copies already packed with the old revision may have to be recalled.")
        out.append(_question(text, "role", role, c["check"], "stock", key, "quality", copies, post.id))
    board.set_status(post.id, "resolved", NAME)
    return out


def _ask_batch_revision(post: Post, ctx: Context) -> list[Post]:
    """The paperwork names no revision: ask the supplier once per part, change nothing about the stock."""
    board: Any = ctx.board
    c = post.body
    _, part_id, batch = (c["key"].split("|") + ["", ""])[:3]
    supplier = fact_table(board).get("receipt", {}).get(batch, {}).get("supplier_id")
    out: list[Post] = []
    topic = f"{c['check']}:supplier_part:{supplier.value}|{part_id}" if supplier is not None else None
    if topic is not None and not any(q.subject == topic for q in board.query(kind="question", subject_prefix=topic)):
        text = (f"{c.get('detail', '')} Which artwork revision did you print for your deliveries of {part_id}?")
        out.append(_question(text, "supplier", str(supplier.value), c["check"], "supplier_part",
                             f"{supplier.value}|{part_id}", "quality", 0, post.id))
    board.set_status(post.id, "resolved", NAME)
    return out


def _quarantine_action(conflict: Post, stock_key: str, detail: str, cause: str = "Failed inspection of") -> Post:
    """Quarantining the fact only changes our plan; this tells the warehouse to stop using the batch."""
    site, part_id, batch = (stock_key.split("|") + ["", ""])[:3]
    act = Action("quarantine_batch", {"site": site, "part_id": part_id, "batch": batch, "reason": detail},
                 NAME, f"{cause} batch {batch}: {detail}", f"quarantine_batch:{site}|{part_id}|{batch}",
                 context_post_ids=(conflict.id,))
    return Post(kind="action", status="requested", subject=act.idempotency_key, body=act.to_dict(),
                author=NAME, depends_on=(conflict.id,))


def _note_agrees(board: Board, receipt_key: str, value: Any) -> bool:
    """True when the receipt's delivery note carries the same quantity."""
    t = fact_table(board)
    note_key = t.get("receipt", {}).get(receipt_key, {}).get("delivery_note")
    note = t.get("delivery_note", {}).get(str(note_key.value), {}).get("qty") if note_key else None
    return note is not None and values_match(note.value, value)


def _steward_fixed(board: Board, entity: str, key: str, field: str) -> bool:
    cur = find_fact_post(board, entity, key, field, ("active",))
    return cur is not None and any(str(x).startswith("steward:") for x in cur.body.get("checks_passed", []))


def _question(text: str, to_kind: str, to_id: str, check: str, entity: str, key: str,
              channel: str, copies: int, conflict_id: str) -> Post:
    q = Question(text=text, to_kind=to_kind, to_id=to_id,  # type: ignore[arg-type]
                 topic=f"{check}:{entity}:{key}", copies_at_risk=copies,
                 channel=channel, context_post_ids=(conflict_id,))  # type: ignore[arg-type]
    return Post(kind="question", status="open", subject=q.topic, body=q.to_dict(), author=NAME,
                depends_on=(conflict_id,))


class DataStewardAgent:
    name = NAME
    subscribes = ("conflict:open",)

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        return steward_conflict(post, ctx)

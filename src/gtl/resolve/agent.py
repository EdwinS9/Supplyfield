"""The resolver: finds and prices fixes for shortfalls and requests them as actions."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from gtl.contracts import Context, Forecast, Option, Post, Question
from gtl.plan import build_plan_input, plan
from gtl.plan.inputs import _DONE, _as_list, _fact_table, _parse_dt, _table, deadline_of
from gtl.resolve.candidates import assume_pending, candidates
from gtl.resolve.pricing import Priced, evaluate, price
from gtl.resolve.sourcing import PREFIXES, sourcing_asks

NAME = "resolver"
_STALE_HOURS = 12
_ASKED = ("open", "asked", "answered")  # a topic is asked once; an expired question may be asked again
_QTY_TEXT = "Has this shipment departed, with what quantity, and when does it arrive?"
_SYSTEM = (
    "You are the resolver of a supply chain control system. You receive a forecast of "
    "shortfalls and a list of priced candidate fixes. Choose which to request now (do), "
    "which to keep as options (hold), and what to ask people (ask). Numbers are computed "
    "by the planner; never change them. Requesting a fix commits nothing: the policy gate "
    "sends it to the person who must approve it and records the answer. So put every fix "
    "you want done in do, also when it needs a sign-off or an approval; use hold only for a "
    "fix you would not do now, and never ask people for an approval yourself. "
    "A candidate's context describes its offer; "
    "promises are not guarantees. Use only candidate ids from the list. "
    "Answer with JSON only: {\"do\": [ids], \"hold\": [ids], \"ask\": [{\"to_kind\": "
    "\"supplier|role|carrier\", \"to_id\": str, \"topic\": str, \"text\": str, "
    "\"channel\": \"supply|quality\"}], \"reason\": {id: one sentence}}."
)
_SCHEMA = {
    "type": "object",
    "properties": {
        "do": {"type": "array", "items": {"type": "string"}},
        "hold": {"type": "array", "items": {"type": "string"}},
        "ask": {"type": "array", "items": {"type": "object", "properties": {
            "to_kind": {"type": "string"}, "to_id": {"type": "string"}, "topic": {"type": "string"},
            "text": {"type": "string"}, "channel": {"type": "string"}}}},
        "reason": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["do"],
}


def _deadline(fc: Forecast, parts: tuple[str, ...]) -> str | None:
    times = [fc.runs_out_at.get(p) for p in parts]
    times = [t for t in times if t]
    return min(times) if times else None


def _end(board: Any, tz: ZoneInfo, pi: Any) -> datetime | None:
    """The earliest deadline of a product still to be packed, else the end of the last plan hour."""
    deadlines = [d for d in (deadline_of(f.get("deadline"), tz) for pid, f in _fact_table(board, "product").items()
                             if pid in pi.products) if d]
    return min(deadlines) if deadlines else (
        _parse_dt(pi.hours[-1], tz) + timedelta(hours=1) if pi.hours else None)


def _eta(f: dict[str, Any], routes: Any, site: Any, tz: ZoneInfo) -> datetime | None:
    """Arrival at the site: the site ETA, else the ETA plus the road leg from the destination."""
    eta = _parse_dt(f.get("eta_site"), tz)
    if eta is None:
        eta = _parse_dt(f.get("eta"), tz)
        dest = f.get("destination")
        if eta is not None and dest and dest != site:
            days = next((float(r["transit_days"]) for r in routes if r.get("origin") == dest
                         and r.get("destination") == site and r.get("transit_days") is not None), 1.0)
            eta += timedelta(days=days)
    return eta


def stale_shipments(board: Any, tenant: dict, now: datetime, fc: Forecast, pi: Any) -> list[dict[str, Any]]:
    """Booked or moving shipments that should arrive in time, with no news for 12+ hours."""
    tz = ZoneInfo(tenant.get("timezone", "UTC"))
    site = tenant.get("site")
    end = _end(board, tz, pi)
    routes = _table(board, "route").values()
    out: list[dict[str, Any]] = []
    for sid, f in sorted(_table(board, "shipment").items()):
        if f.get("status") not in ("booked", "in_transit", "delayed") or end is None:
            continue
        eta = _eta(f, routes, site, tz)
        updated = _parse_dt(f.get("status_updated_at"), tz)
        if eta is None or eta >= end or updated is None or now - updated <= timedelta(hours=_STALE_HOURS):
            continue
        part = next((p for p in _as_list(f.get("part_ids")) if fc.shadow_price.get(p, 0.0) > 0), None)
        supplier = f.get("supplier_id")
        if part is None or not supplier:
            continue
        out.append({"shipment_id": sid, "supplier_id": str(supplier), "part_id": part,
                    "eta": eta.isoformat(), "status_updated_at": updated.isoformat()})
    return out


def information_asks(board: Any, tenant: dict, fc: Forecast, pi: Any, priced: list[Priced]) -> list[dict[str, Any]]:
    """Sourcing RFQs and shipment evidence needed for the current shortage."""
    tz = ZoneInfo(tenant.get("timezone", "UTC"))
    site = tenant.get("site")
    tight = {p for p, v in fc.shadow_price.items() if v > 0} | {p for p in fc.limiting_part.values() if p}
    # A useful on-time option may solve this material even when another part still limits the product.
    fixes = [option.candidate for option in priced if option.worth_it]
    after = evaluate(pi, fixes) if fixes else fc
    unmet = {part for product, gap in after.shortfall.items() if gap > 0
             for part, units in pi.bom.get(product, {}).items() if units > 0}
    remaining = {p for p, value in after.shadow_price.items() if value > 0 and p in unmet}
    remaining.update(p for p in after.limiting_part.values() if p)
    end = _end(board, tz, pi)
    gaps = {part: sum(gap * pi.bom.get(product, {}).get(part, 0) for product, gap in after.shortfall.items())
            for part in tight & remaining}
    out = sourcing_asks(board, tenant, gaps, end)
    routes = _table(board, "route").values()
    for sid, f in sorted(_table(board, "shipment").items()):
        eta = _eta(f, routes, site, tz)
        part = next((p for p in _as_list(f.get("part_ids")) if p in tight), None)
        supplier = f.get("supplier_id")
        if (f.get("status") in _DONE or f.get("qty_shipped") is not None or part is None or not supplier
                or eta is None or end is None or eta >= end):
            continue
        out.append({"to_kind": "supplier", "to_id": str(supplier), "topic": f"shipment:{sid}", "text": _QTY_TEXT,
                    "channel": "supply", "part": part})
    return out


def _body_numbers(b: dict[str, Any]) -> tuple:
    return (round(float(b.get("cost_eur", 0)), 2), round(float(b.get("net_value_eur", 0)), 2),
            tuple(sorted((k, round(float(v), 2)) for k, v in b.get("copies_saved", {}).items())),
            b.get("unpriced_reason"))


def _option_for(p: Priced) -> Option:
    c = p.candidate
    return Option(c.title, c.actions, dict(p.copies_saved), c.cost_eur or 0.0, p.net_value_eur, c.needs)


def _llm_plan(ctx: Context, fc: Forecast, pi: Any, priced: list[Priced],
              stale: list[dict[str, Any]]) -> dict[str, Any] | None:
    user = {
        "forecast": {"copies": fc.copies, "shortfall": fc.shortfall, "limiting_part": fc.limiting_part,
                     "runs_out_at": fc.runs_out_at, "hours_left": len(pi.hours)},
        "candidates": [{
            "id": p.candidate.id, "title": p.candidate.title, "cost_eur": p.candidate.cost_eur,
            "copies_saved": p.copies_saved, "standalone_copies_saved": p.standalone_copies_saved,
            "net_value_eur": p.net_value_eur, "standalone_net_value_eur": p.standalone_net_value_eur,
            "worth_it": p.worth_it, "rank": p.rank, "needs": p.candidate.needs,
            "unpriced_reason": p.candidate.unpriced_reason, "context": p.candidate.context} for p in priced],
        "stale_shipments": stale,
    }
    system = _SYSTEM
    playbook = ctx.playbooks.get("resolver", "")
    if playbook:
        system += "\n\n" + playbook
    try:
        res = ctx.llm.complete("agent", system, json.dumps(user, default=str), schema=_SCHEMA)
    except Exception:  # noqa: BLE001 - any model failure means the deterministic fallback
        return None
    data = res.data
    if not isinstance(data, dict) or not isinstance(data.get("do"), list):
        return None
    return data


DECISION_EVENT = "resolver.decision"


def _problem(priced: list[Priced], stale: list[dict[str, Any]]) -> dict[str, Any]:
    """What the decision is about: the candidates with their worth-it flags, and the stale shipments."""
    return {"candidates": sorted([p.candidate.id, p.worth_it] for p in priced),
            "stale": sorted(s["shipment_id"] for s in stale)}


def _decide(ctx: Context, fc: Forecast, pi: Any, priced: list[Priced],
            stale: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The model's plan, asked for only when the problem changed since the last time; else the last answer.

    The last answer lives on the Board as an event, so no state sits on the agent (it is shared by episodes).
    """
    problem = _problem(priced, stale)
    last = next((e["payload"] for e in reversed(ctx.board.events_since(0)) if e["type"] == DECISION_EVENT), None)
    if last is not None and last["problem"] == problem:
        return last["data"]
    data = _llm_plan(ctx, fc, pi, priced, stale)
    ctx.board.log_event(NAME, DECISION_EVENT, {"problem": problem, "data": data})
    return data


class ResolverAgent:
    name = NAME
    subscribes = ("forecast:current", "question:answered", "question:expired")

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        sourcing_update = post.kind == "question"
        if sourcing_update:
            if not str(post.body.get("topic", "")).startswith(("spare:", "recovery:")):
                return []
            current = ctx.board.query(kind="forecast", status="current")
            if not current:
                return []
            post = current[-1]
        board = ctx.board
        now = ctx.ports.clock.now()
        # Price on top of our own pending actions, which are not executed yet.
        pi = assume_pending(board, ctx.tenant, now, build_plan_input(board, ctx.tenant, now))
        fc = plan(pi)
        cands = candidates(board, ctx.tenant, now, fc, pi)
        if not cands and all(s <= 0.5 for s in fc.shortfall.values()):
            return []
        priced = price(cands, pi, ctx.tenant)
        information = information_asks(board, ctx.tenant, fc, pi, priced)
        if sourcing_update:
            return self._questions(information, board, fc, post)
        stale = stale_shipments(board, ctx.tenant, now, fc, pi)
        data = _decide(ctx, fc, pi, priced, stale)
        by_id = {p.candidate.id: p for p in priced}
        if data is None:
            do = [p.candidate.id for p in priced if p.worth_it]
            reasons: dict[str, str] = {}
            asks = [{"to_kind": "supplier", "to_id": s["supplier_id"], "topic": f"shipment:{s['shipment_id']}",
                     "text": _QTY_TEXT, "channel": "supply", "part": s["part_id"]} for s in stale]
        else:
            do = [i for i in data["do"] if isinstance(i, str) and i in by_id
                  and by_id[i].candidate.cost_eur is not None]
            r = data.get("reason")
            reasons = {k: v for k, v in r.items() if k in by_id and isinstance(v, str)} if isinstance(r, dict) else {}
            asks = data.get("ask") if isinstance(data.get("ask"), list) else []
        do = list(dict.fromkeys(do + [p.candidate.id for p in priced if p.candidate.always]))
        asks = [*information, *(a for a in asks if isinstance(a, dict) and not str(a.get("topic", "")).startswith(PREFIXES))]

        out: list[Post] = []
        existing = board.query(kind="option", limit=100_000)
        for p in priced:
            cid = p.candidate.id
            opt = _option_for(p)
            body = opt.to_dict()
            if p.candidate.cost_eur is None:
                body["unpriced_reason"] = p.candidate.unpriced_reason
            if p.candidate.context:
                body["context"] = dict(p.candidate.context)  # for the trace and for reflection
            same = [o for o in existing if o.subject == cid and o.status in ("proposed", "chosen")]
            fresh = [o for o in same if _body_numbers(o.body) == _body_numbers(body)]
            if fresh:
                option_id = fresh[0].id
            else:
                for o in same:
                    if o.status == "proposed":
                        board.set_status(o.id, "discarded", self.name, "numbers changed")
                option_id = board.append(Post(kind="option", status="proposed", subject=cid, body=body,
                                              author=self.name, depends_on=(post.id,))).id
            if cid not in do:
                continue
            if not fresh or fresh[0].status == "proposed":
                board.set_status(option_id, "chosen", self.name, "requested")
            cost_text = (f"cost {p.candidate.cost_eur:,.0f} EUR, net {p.net_value_eur:,.0f} EUR"
                         if p.candidate.cost_eur is not None
                         else f"not priced: {p.candidate.unpriced_reason}")
            reason = reasons.get(cid) or (
                f"{p.candidate.title}. Saves {sum(p.copies_saved.values()):g} copies, {cost_text}.")
            dl = _deadline(fc, p.candidate.parts)
            for a in p.candidate.actions:
                params = {**a.params, "copies_saved": dict(p.copies_saved)}
                if dl:
                    params["deadline"] = dl
                act = replace(a, params=params, reason=reason, context_post_ids=(option_id,))
                out.append(Post(kind="action", status="requested", subject=act.idempotency_key,
                                body=act.to_dict(), author=self.name, depends_on=(post.id,)))

        out += self._questions(asks, board, fc, post)
        return out

    def _questions(self, asks: list[Any], board: Any, fc: Forecast, post: Post) -> list[Post]:
        busy = {(q.body.get("to_id"), q.body.get("topic"))
                for s in _ASKED for q in board.query(kind="question", status=s, limit=100_000)}
        at_risk = int(sum(fc.shortfall.values()))
        out: list[Post] = []
        for a in asks:
            if not isinstance(a, dict):
                continue
            kind, to_id, topic, text = (a.get(k) for k in ("to_kind", "to_id", "topic", "text"))
            if kind not in ("supplier", "role", "carrier") or not all(
                    isinstance(x, str) and x for x in (to_id, topic, text)):
                continue
            if (to_id, topic) in busy:
                continue
            busy.add((to_id, topic))
            channel = a.get("channel") if a.get("channel") in ("supply", "quality") else "supply"
            q = Question(text=text, to_kind=kind, to_id=to_id, topic=topic, copies_at_risk=at_risk,
                         deadline=_deadline(fc, (a["part"],)) if a.get("part") else None,
                         channel=channel, context_post_ids=(post.id,))
            out.append(Post(kind="question", status="open", subject=f"{to_id}|{topic}",
                            body=q.to_dict(), author=self.name, depends_on=(post.id,)))
        return out

"""Why did the team miss a problem? One cause category per missed remedy (docs/gym.md section 12).

`diagnose` is a pure function over an episode's result, its scenario and the Board it left behind. It
walks the chain a fix has to travel and names the first link that broke:

    not_visible   the evidence was only available on request and nobody asked
    no_conflict   the evidence was on the Board but no conflict or correction followed
    no_candidate  the problem was known but the resolver generated no fix
    not_worth_it  a fix was generated but priced at zero or negative
    not_chosen    a fix was priced as worth it but nobody requested it
    not_approved  requested, but blocked, rejected or still waiting for a person
    too_late      executed, but the effect did not land before the deadline
    floor_ignored our plan changed but the warehouse kept doing something else
    trusted_promise the team accepted an offer that arrived late while an on-time alternative existed

The output is plain data (lists of dicts) so the learning loop can use it as it is.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from gtl.contracts import Action, Board
from gtl.gym.score import Result
from gtl.gym.scenario import Scenario
from gtl.gym.world import matches_remedy

CAUSES = ("not_visible", "no_conflict", "no_candidate", "not_worth_it", "not_chosen", "not_approved",
          "too_late", "floor_ignored", "trusted_promise")
# Actions that change what we believe or request, but that only a request carries to the warehouse.
BELIEF_ONLY = ("quarantine_batch", "set_priority")
# How far an action got, so the most advanced one explains a miss.
_STAGE = {"failed": 0, "blocked": 0, "rejected": 0, "requested": 1, "needs_approval": 2, "allowed": 3,
          "approved": 3, "executed": 4}


def _action(body: dict[str, Any]) -> Action:
    return Action(**{k: v for k, v in body.items() if k in Action.__dataclass_fields__})


def _claim_on_board(board: Board, entity: str, key: str, field: str, value: Any) -> bool:
    subject = f"{entity}/{key}/{field}"
    return any(p.subject == subject and str(p.body.get("value")) == str(value)
               for p in board.query(kind="claim", subject_prefix=subject, limit=1_000_000))


def _hidden(board: Board, claims: list[Any]) -> list[str]:
    """Slots of on-request evidence that never reached the Board."""
    return [f"{c.entity}/{c.key}/{c.field}" for c in claims
            if "on_request" in c.flags and not _claim_on_board(board, c.entity, c.key, c.field, c.value)]


def _conflicts(board: Board, check: str | None = None) -> list[Any]:
    return [p for p in board.query(kind="conflict", limit=1_000_000) if check is None or p.body["check"] == check]


def _quarantined_batch(board: Board, batch: str) -> bool:
    return any(str(p.body.get("key", "")).rsplit("|", 1)[-1] == batch and p.body.get("field") == "qty"
               for p in board.query(kind="fact", status="quarantined", subject_prefix="stock/", limit=1_000_000))


def _known(board: Board, label: dict) -> bool:
    """Did the Board react to the evidence: a conflict, a correction or the fact a fix is built on?"""
    t = label["target"]
    kind = label["type"]
    if kind in ("booking_error", "rival_offers"):
        batch = str(t["batch"])
        return any(batch in str(p.body.get("key")) for p in _conflicts(board)) or _quarantined_batch(board, batch)
    if kind == "failed_lot":
        batch = str(t["batch"])
        return any(str(p.body.get("key")).endswith(batch) for p in _conflicts(board, "inspection_vs_stock")) \
            or _quarantined_batch(board, batch)
    if kind == "short_shipment":
        return any(p.body.get("key") == t["shipment_id"] for p in _conflicts(board, "shipment_vs_advice"))
    if kind == "terms_hold":
        return any(f.field == "hold_reason" and f.value and f.key.startswith(f"{t['po_number']}|")
                   for f in board.facts("order_confirmation"))
    if kind == "stuck_at_port":
        return any(f.key == t["shipment_id"] and f.field == "status" and f.value == "arrived_port"
                   for f in board.facts("shipment"))
    return True


def _entry(label: dict, remedy: Any, cause: str, detail: str, missed: bool, costly: bool) -> dict[str, Any]:
    return {"problem": label["id"], "type": label["type"], "remedy": remedy.id if remedy else None,
            "kind": remedy.match.get("kind") if remedy else None, "cause": cause, "detail": detail,
            "problem_missed": missed, "costly": costly}


def _why(board: Board, label: dict, remedy: Any, hidden: list[str], rejected: set[str]) -> tuple[str, str]:
    """The cause and a plain sentence for one remedy that never took effect."""
    kind = remedy.match.get("kind")
    actions = [(p, _action(p.body)) for p in board.query(kind="action", limit=1_000_000)]
    mine = [(p, a) for p, a in actions if matches_remedy(remedy, a)]
    if mine:
        post = max((p for p, _ in mine), key=lambda p: _STAGE.get(p.status, 0))
        if post.status == "executed" and remedy.id not in rejected:
            return "too_late", f"{kind} was executed at {post.created_at} but its effect did not land in time"
        return "not_approved", f"{kind} ended as {post.status}" + (
            ", and the person who decides turned it down" if remedy.id in rejected else "")
    if kind == "quarantine_batch" and _quarantined_batch(board, str(label["target"].get("batch"))):
        return "floor_ignored", "the Board quarantined the batch but no action told the warehouse to stop using it"
    for option in board.query(kind="option", limit=1_000_000):
        if any(matches_remedy(remedy, _action(a)) for a in option.body.get("actions", ())):
            net = float(option.body.get("net_value_eur", 0.0))
            if net > 0:
                return "not_chosen", f"{option.subject} was priced at {net:,.0f} EUR but never requested"
            if kind in BELIEF_ONLY:
                return "floor_ignored", f"{option.subject} changes nothing in the plan, so it was dropped, " \
                                        "and the warehouse kept its old order"
            return "not_worth_it", f"{option.subject} was priced at {net:,.0f} EUR"
    if kind == "book_freight":
        return "no_candidate", "no agent can see freight options: no claim carries them, so no fix is generated"
    if hidden:
        return "not_visible", f"evidence only on request, nobody asked: {', '.join(hidden)}"
    if not _known(board, label):
        return "no_conflict", "the evidence is on the Board but no conflict or correction followed"
    return "no_candidate", "the problem is known on the Board but the resolver generated no fix"


def diagnose(result: Result, scenario: Scenario, board: Board) -> list[dict[str, Any]]:
    """One entry per remedy the oracle used that did not take effect, with its cause category.

    `costly` is true for these: the miss cost euros, because the oracle uses the remedy. A problem the team
    missed whose remedies the oracle does not use gets one entry for its first remedy with `costly` false.
    """
    world = scenario.build()
    wanted = set(result.breakdown.get("oracle_remedies", ()))
    done = set(result.breakdown.get("scheduled_remedies", ()))
    rejected = set(result.breakdown.get("rejected_remedies", ()))
    missed = set(result.problems_missed)
    out: list[dict[str, Any]] = []
    for d in scenario.disruptions:
        label = d.labels()
        if label["decoy"]:
            continue
        hidden = _hidden(board, [c for _, c in d.observables(world)])
        every = d.remedies(world)
        remedies = [r for r in every if r.id in wanted]
        costly = bool(remedies)
        if not remedies and label["id"] in missed:
            remedies = every[:1]
        if not remedies and label["id"] in missed:
            out.append(_entry(label, None, "no_candidate", "the problem has no remedy at all", True, False))
        for r in remedies:
            if r.id not in done:
                cause, detail = _why(board, label, r, hidden, rejected)
                out.append(_entry(label, r, cause, detail, label["id"] in missed, costly))
    out += _trusted_promises(result, scenario, world)
    return out


def _trusted_promises(result: Result, scenario: Scenario, world: Any) -> list[dict[str, Any]]:
    """Accepted offers that slipped although another supplier's offer for the same part would have been on time."""
    slips = world.truth.get("slips", {})
    offers = [(r, r.offer) for r in world.truth["remedies"] if r.offer]
    out: list[dict[str, Any]] = []
    for o in result.breakdown.get("outcomes", ()):
        if o["late_hours"] <= 0:
            continue
        mine = next((r for r, f in offers if f["supplier_id"] == o["supplier_id"] and f["part_id"] == o["part_id"]),
                    None)
        alt = next((f["supplier_id"] for r, f in offers if f["part_id"] == o["part_id"]
                    and f["supplier_id"] != o["supplier_id"] and not slips.get(f["supplier_id"], 0.0)), None)
        if mine is None or alt is None:
            continue
        label = next(d.labels() for d in scenario.disruptions if d.id == mine.disruption_id)
        detail = (f"accepted an offer from {o['supplier_id']} (on-time rate {o['supplier_on_time_rate']:g}) that "
                  f"arrived {o['late_hours']:g} hours late; the offer from {alt} would have been on time")
        out.append(_entry(label, mine, "trusted_promise", detail, label["id"] in set(result.problems_missed), True))
    return out


def causes_table(entries: list[dict[str, Any]], costly_only: bool = False) -> dict[str, int]:
    """Count of misses per cause over any number of episodes, every category present."""
    counts = Counter(e["cause"] for e in entries if e["costly"] or not costly_only)
    return {c: counts.get(c, 0) for c in CAUSES}


def below_do_nothing(result: Result, board: Board) -> dict[str, Any] | None:
    """Why a team that did worse than doing nothing lost money: the costs it caused and the actions that backfired."""
    if result.reward >= (result.do_nothing or 0.0) - 1e-6:
        return None
    terms = result.breakdown.get("terms", {})
    costs = {k: round(terms[k], 2) for k in ("spend", "recall", "fraud", "violations", "human_items", "emails")
             if terms.get(k)}
    bad = [p.body["kind"] for p in board.query(kind="action", limit=1_000_000) if p.status in ("blocked", "rejected")]
    parts = [f"{k} {v:,.0f} EUR" for k, v in costs.items()]
    if result.breakdown.get("bad_unit_batches"):
        parts.append("packed units from a failed lot: " + ", ".join(result.breakdown["bad_unit_batches"]))
    if bad:
        parts.append("actions turned down: " + ", ".join(sorted(bad)))
    return {"short_by_eur": round((result.do_nothing or 0.0) - result.reward, 2), "costs": costs,
            "reason": "; ".join(parts) or "fewer copies were packed than with no agent at all"}


def _hour(world_start: datetime, at: str) -> float:
    return round((datetime.fromisoformat(at) - world_start).total_seconds() / 3600.0, 1)


def episode_report(result: Result, scenario: Scenario, board: Board) -> dict[str, Any]:
    """Everything one episode's diagnosis prints, as data."""
    world = scenario.build()
    actions = []
    for p in board.query(kind="action", limit=1_000_000):
        if p.body.get("kind") == "send_email":
            continue
        actions.append({"kind": p.body["kind"], "status": p.status, "key": p.body.get("idempotency_key"),
                        "hour": _hour(world.start, p.created_at), "cost_eur": p.body.get("cost_eur", 0.0)})
    return {
        "scenario": scenario.id,
        "disruptions": [d.labels()["id"] for d in scenario.disruptions],
        "do_nothing": result.do_nothing, "team": result.reward, "oracle": result.oracle, "score": result.score,
        "stake_eur": (result.oracle or 0.0) - (result.do_nothing or 0.0),
        "below_do_nothing": below_do_nothing(result, board),
        "oracle_remedies": result.breakdown.get("oracle_remedies", []),
        "scheduled_remedies": result.breakdown.get("scheduled_remedies", []),
        "actions": actions,
        "problems_found": result.problems_found, "problems_missed": result.problems_missed,
        "misses": diagnose(result, scenario, board),
    }

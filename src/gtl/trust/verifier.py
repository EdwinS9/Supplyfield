"""The verifier: plain code that decides what becomes a fact."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from gtl.contracts import COUNTERS, SOURCE_PRIORITY, Board, Conflict, Context, Fact, Post
from gtl.trust.batch_revision import derive_revisions
from gtl.trust.checks import CHECKS
from gtl.trust.common import (conflict_subject, evidence_sources, find_fact_post, pick_preferred,
                              slot_subject, to_number, parse_ts, values_match)

NAME = "verifier"
MIN_CONFIDENCE = 0.5


def reject_reason(body: dict[str, Any]) -> str | None:
    """The failed check name for a claim that must be rejected, else None."""
    ev = body.get("evidence") or []
    if not ev or (body.get("source_kind") == "document" and not any(e.get("quote") for e in ev)):
        return "evidence_present"
    flags = body.get("flags") or []
    if "low_confidence" in flags or float(body.get("confidence", 1.0)) < MIN_CONFIDENCE:
        return "low_confidence"
    return None


def _claim_bodies(board: Board, ids: tuple[str, ...] | list[str]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for cid in ids:
        p = board.get(cid)
        if p is not None:
            out.append((cid, p.body))
    return out


def new_fact(claim_id: str, claim: Post, supersedes: str | None = None) -> Post:
    b = claim.body
    fact = Fact(entity=b["entity"], key=b["key"], field=b["field"], value=b["value"], as_of=b["as_of"],
                claim_ids=(claim_id,), checks_passed=("evidence_present", "low_confidence"),
                unit=b.get("unit"))
    deps = tuple(dict.fromkeys((claim_id, *claim.depends_on)))
    return Post(kind="fact", status="active", subject=slot_subject(fact.entity, fact.key, fact.field),
                body=fact.to_dict(), author=NAME, depends_on=deps, supersedes=supersedes)


def verify_claim(post: Post, ctx: Context) -> list[Post]:
    board: Any = ctx.board
    b = post.body
    if reject_reason(b):
        board.set_status(post.id, "rejected", NAME, reject_reason(b) or "")
        return []
    if "struck_out" in (b.get("flags") or []):
        board.set_status(post.id, "verified", NAME, "struck out: evidence only")
        return []
    existing = find_fact_post(board, b["entity"], b["key"], b["field"], ("active", "quarantined"))
    if existing is None:
        board.set_status(post.id, "verified", NAME)
        return [new_fact(post.id, post)]
    if values_match(existing.body.get("value"), b["value"]):
        body = dict(existing.body)
        body["claim_ids"] = list(dict.fromkeys([*body.get("claim_ids", []), post.id]))
        board.update_body(existing.id, body, NAME)
        board.set_status(post.id, "verified", NAME)
        return []
    if existing.status == "active" and (b["entity"], b["field"]) in COUNTERS:
        out = _counter_reading(board, post, existing)
        if out is not None:
            return out
    if existing.status == "active":
        carried = _carry_correction(board, post, existing)
        if carried is not None:
            return carried
        # A counter that dropped stays a conflict, so counters are excluded from the dated rule.
        dated = None if (b["entity"], b["field"]) in COUNTERS else _dated_update(board, post, existing)
        if dated is not None:
            return dated
    if _known_dispute(board, b):
        board.set_status(post.id, "conflicted", NAME, "known dispute")
        return []
    old_sources: set[str] = set()
    for _, cb in _claim_bodies(board, existing.body.get("claim_ids", [])):
        old_sources |= evidence_sources(cb)
    if evidence_sources(b) & old_sources:
        board.set_status(existing.id, "superseded", NAME, f"replaced by {post.id}")
        board.set_status(post.id, "verified", NAME)
        return [new_fact(post.id, post, supersedes=existing.id)]
    ids = (post.id, *existing.body.get("claim_ids", []))
    if _conflict_exists(board, conflict_subject("cross_source", b["entity"], b["key"], b["field"]), ids):
        board.set_status(post.id, "conflicted", NAME, "duplicate conflict")
        return []
    preferred = pick_preferred([(post.id, b), *_claim_bodies(board, existing.body.get("claim_ids", []))])
    board.set_status(post.id, "conflicted", NAME)
    return [_conflict_post(Conflict(
        b["entity"], b["key"], b["field"], ids, "cross_source",
        f"Sources disagree: {existing.body.get('value')} against {b['value']}.", preferred))]


def _rank(kind: Any) -> int:
    return SOURCE_PRIORITY.index(kind) if kind in SOURCE_PRIORITY else len(SOURCE_PRIORITY)


def _dated_update(board: Any, post: Post, existing: Post) -> list[Post] | None:
    """A claim dated on a later calendar day, from a source not ranked lower, supersedes: not a contradiction.

    Price lists with a valid-from date and daily updates differ from older values by design. Skipped when
    the steward wrote the active fact. Returns None when the rule does not apply.
    """
    b = post.body
    new_t, old_t = parse_ts(b.get("as_of")), parse_ts(existing.body.get("as_of"))
    if new_t is None or old_t is None or new_t.date() <= old_t.date():
        return None
    if any(str(c).startswith("steward:") for c in existing.body.get("checks_passed", [])):
        return None
    kinds = [cb.get("source_kind") for _, cb in _claim_bodies(board, existing.body.get("claim_ids", []))]
    if not kinds or _rank(b.get("source_kind")) > min(_rank(k) for k in kinds):
        return None
    board.set_status(existing.id, "superseded", NAME, f"replaced by dated claim {post.id}")
    board.set_status(post.id, "verified", NAME)
    return [new_fact(post.id, post, supersedes=existing.id)]


def _counter_reading(board: Any, post: Post, existing: Post) -> list[Post] | None:
    """A counter only grows: a newer, not lower reading supersedes; an older, not higher one is history.

    Returns None when neither holds, so the cross-source path decides.
    """
    b = post.body
    new_t, old_t = parse_ts(b.get("as_of")), parse_ts(existing.body.get("as_of"))
    new_v, old_v = to_number(b["value"]), to_number(existing.body.get("value"))
    if None in (new_t, old_t, new_v, old_v) or new_t == old_t:
        return None
    if new_t > old_t and new_v >= old_v:
        board.set_status(existing.id, "superseded", NAME, f"replaced by {post.id}")
        board.set_status(post.id, "verified", NAME)
        return [new_fact(post.id, post, supersedes=existing.id)]
    if new_t < old_t and new_v <= old_v:
        board.set_status(post.id, "verified", NAME, "older reading kept as history")
        return [replace(new_fact(post.id, post), status="superseded")]
    return None


def _carry_correction(board: Any, post: Post, active: Post) -> list[Post] | None:
    """The steward corrected a number; the same source now reports a different raw value.

    The correction is a difference (steward value minus quarantined value), so it is
    applied to the new raw value instead of being lost.
    """
    b = post.body
    subject = slot_subject(b["entity"], b["key"], b["field"])
    if not any(str(c).startswith("steward:") for c in active.body.get("checks_passed", [])):
        return None  # only a corrected value carries a difference, also after it was carried once
    quarantined = None
    for q in board.query(kind="fact", status="quarantined", subject_prefix=subject, limit=100_000):
        if q.subject != subject:
            continue
        sources: set[str] = set()
        for _, cb in _claim_bodies(board, q.body.get("claim_ids", [])):
            sources |= evidence_sources(cb)
        if evidence_sources(b) & sources:
            quarantined = q
    if quarantined is None:
        return None
    claim_v, bad_v, fixed_v = (to_number(x) for x in (b["value"], quarantined.body.get("value"),
                                                     active.body.get("value")))
    if None in (claim_v, bad_v, fixed_v) or values_match(claim_v, bad_v):
        return None
    ab = active.body
    fact = Fact(entity=b["entity"], key=b["key"], field=b["field"], value=max(0.0, claim_v + (fixed_v - bad_v)),
                as_of=b["as_of"], claim_ids=(*ab.get("claim_ids", []), post.id),
                checks_passed=tuple(dict.fromkeys((*ab.get("checks_passed", []), "carried_correction"))),
                unit=ab.get("unit"))
    board.update_body(quarantined.id, {**quarantined.body, "value": b["value"],
                                       "claim_ids": list(dict.fromkeys([*quarantined.body.get("claim_ids", []),
                                                                         post.id]))}, NAME)
    board.set_status(active.id, "superseded", NAME, f"carried correction by {post.id}")
    board.set_status(post.id, "conflicted", NAME, "known dispute")
    deps = tuple(dict.fromkeys((post.id, *active.depends_on, *post.depends_on)))
    return [Post(kind="fact", status="active", subject=subject, body=fact.to_dict(), author=NAME,
                 depends_on=deps, supersedes=active.id)]


def _known_dispute(board: Board, b: dict[str, Any]) -> bool:
    """The claim restates a quarantined value from a source already behind that value."""
    subject = slot_subject(b["entity"], b["key"], b["field"])
    for q in board.query(kind="fact", status="quarantined", subject_prefix=subject, limit=100_000):
        if q.subject != subject or not values_match(q.body.get("value"), b["value"]):
            continue
        sources: set[str] = set()
        for _, cb in _claim_bodies(board, q.body.get("claim_ids", [])):
            sources |= evidence_sources(cb)
        if evidence_sources(b) & sources:
            return True
    return False


def _conflict_exists(board: Board, subject: str, ids: tuple[str, ...]) -> bool:
    return any(p.subject == subject and set(p.body.get("claim_ids", [])) == set(ids)
               for p in board.query(kind="conflict", subject_prefix=subject, limit=100_000))


def _conflict_post(c: Conflict) -> Post:
    deps = tuple(dict.fromkeys(c.claim_ids))
    return Post(kind="conflict", status="open", subject=conflict_subject(c.check, c.entity, c.key, c.field),
                body=c.to_dict(), author=NAME, depends_on=deps)


def run_checks(board: Board, now: datetime) -> list[Post]:
    """Conflicts from every registered check; a subject that exists in any status is skipped."""
    out: list[Post] = []
    seen = {p.subject for p in board.query(kind="conflict", limit=1_000_000)}
    for check in CHECKS:
        for c in check(board, now):
            post = _conflict_post(c)
            if post.subject not in seen:
                seen.add(post.subject)
                out.append(post)
    return out


class VerifierAgent:
    name = NAME
    subscribes = ("claim:proposed", "event:settled")

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        if post.kind == "claim":
            return verify_claim(post, ctx)
        for derived in derive_revisions(ctx.board, NAME):
            ctx.board.append(derived)  # appended now so the checks below already see the revision
        return run_checks(ctx.board, ctx.ports.clock.now())

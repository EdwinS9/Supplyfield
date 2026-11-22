"""The propagator: keeps beliefs consistent when sources change.

Plain code, no LLM. Two jobs:
- a changed or removed file makes everything that depended on the old version stale;
- a new forecast that moved is explained as a belief diff.
"""
from __future__ import annotations

from typing import Any

from gtl.contracts import STATUSES, BeliefDiff, Context, Post

NAME = "propagator"

# Statuses after which a post is finished and must not be touched.
FINAL: dict[str, tuple[str, ...]] = {
    "claim": ("rejected", "stale"),
    "fact": ("superseded", "stale"),
    "conflict": ("resolved", "stale"),
    "defect": ("fixed", "dismissed", "stale"),
    "question": ("answered", "expired", "stale"),
    "option": ("discarded", "stale"),
    "action": ("executed", "rejected", "blocked", "failed", "stale"),
    "decision": ("answered", "expired", "stale"),
}
TRIGGER_EVENTS = ("file.changed", "mail.received", "human.answered")
MOVE_THRESHOLD = 0.5
MAX_CHANGED = 20


# -- stale propagation -------------------------------------------------------
def dependents_closure(board: Any, ref: str) -> list[str]:
    """Post ids that depend on `ref`, directly or through other posts, in dependency order."""
    order: list[str] = []
    seen: set[str] = set()
    queue = list(board.dependents(ref))
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        order.append(pid)
        queue.extend(board.dependents(pid))
    return order


def mark_stale(board: Any, ref: str) -> list[str]:
    """Set every non-final dependent of `ref` to stale. Returns the ids changed."""
    changed: list[str] = []
    for pid in dependents_closure(board, ref):
        p = board.get(pid)
        if p is None or p.kind in ("event", "forecast"):
            continue
        if "stale" not in STATUSES.get(p.kind, ()):
            continue  # this kind has no stale status
        if p.status in FINAL.get(p.kind, ()):
            continue
        board.set_status(pid, "stale", NAME, note=f"source changed: {ref}")
        changed.append(pid)
    return changed


# -- belief diffs ------------------------------------------------------------
def _num(x: float) -> str:
    return f"{x:,.0f}"


def _product_name(board: Any, product_id: str) -> str:
    for f in board.facts("product", key_prefix=product_id):
        if f.key == product_id and f.field == "name":
            return str(f.value)
    return product_id


def _describe_change(c: dict[str, Any]) -> str:
    entity, key, field = c["slot"]
    if entity == "stock" and field == "qty":
        parts = str(key).split("|")
        name = parts[1] if len(parts) > 1 else key
        return f"stock of {name} {_fmt(c['before'])} -> {_fmt(c['after'])}"
    return f"{entity} {key} {field} {_fmt(c['before'])} -> {_fmt(c['after'])}"


def _fmt(v: Any) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return _num(v)
    return "none" if v is None else str(v)


def _event_id_of(board: Any, post_id: str) -> int:
    for ev in board.events_since(0):
        if ev["type"] == "post.append" and ev["post_id"] == post_id:
            return int(ev["id"])
    return 0


def _changed_facts(board: Any, since: int) -> list[dict[str, Any]]:
    by_slot: dict[tuple[str, str, str], dict[str, Any]] = {}
    for ev in board.events_since(since):
        if ev["type"] not in ("post.append", "post.status") or not ev["post_id"]:
            continue
        p = board.get(ev["post_id"])
        if p is None or p.kind != "fact":
            continue
        b = p.body
        slot = (b.get("entity"), b.get("key"), b.get("field"))
        if ev["type"] == "post.append":
            prev = board.get(p.supersedes) if p.supersedes else None
            before = prev.body.get("value") if prev else None
            after = b.get("value")
        else:
            before, after = ev["payload"].get("from"), ev["payload"].get("to")
            if after == "superseded":
                continue  # the new fact that replaced it carries the change
        if slot in by_slot:
            before = by_slot[slot]["before"]
        if before == after:
            by_slot.pop(slot, None)
            continue
        by_slot[slot] = {"slot": list(slot), "before": before, "after": after}
    return list(by_slot.values())[:MAX_CHANGED]


def _trigger(board: Any, since: int, fallback: str) -> tuple[str, str]:
    """Newest trigger event post since `since`, and a short phrase about it."""
    found: tuple[str, str] | None = None
    for ev in board.events_since(since):
        if ev["type"] != "post.append" or not ev["post_id"]:
            continue
        sub = (ev["payload"] or {}).get("subject")
        if sub in TRIGGER_EVENTS and (ev["payload"] or {}).get("kind") == "event":
            p = board.get(ev["post_id"])
            if sub == "file.changed":
                what = f"{p.body.get('path', 'a file')} changed" if p else "a file changed"
            elif sub == "mail.received":
                what = "a supplier mail arrived"
            else:
                what = "a person answered"
            found = (ev["post_id"], what)
    return found or (fallback, "the plan was recomputed")


def belief_diff(board: Any, post: Post) -> Post | None:
    """Compare a new forecast with the previous one; None when nothing moved."""
    prevs = [p for p in board.query(kind="forecast", status="superseded") if p.id != post.id]
    if not prevs:
        return None
    prev = prevs[-1]
    before = dict(prev.body.get("copies") or {})
    after = dict(post.body.get("copies") or {})
    moved = [pid for pid in sorted(set(before) | set(after))
             if abs(float(after.get(pid, 0)) - float(before.get(pid, 0))) > MOVE_THRESHOLD]
    if not moved:
        return None
    since = _event_id_of(board, prev.id)
    changed = _changed_facts(board, since)
    trigger, what = _trigger(board, since, post.id)
    parts = []
    for pid in moved:
        b, a = float(before.get(pid, 0)), float(after.get(pid, 0))
        verb = "fell" if a < b else "rose"
        parts.append(f"{_product_name(board, pid)} forecast {verb} from {_num(b)} to {_num(a)}")
    summary = "; ".join(parts) + f" after {what}"
    if changed:
        summary += ": " + ", ".join(_describe_change(c) for c in changed[:3])
    diff = BeliefDiff(trigger=trigger, changed=tuple(changed), forecast_before=before,
                      forecast_after=after, summary=summary)
    return Post(kind="belief_diff", status="posted", subject=summary[:120], body=diff.to_dict(),
                author=NAME, depends_on=(post.id, prev.id))


class PropagatorAgent:
    name = NAME
    subscribes = ("event:file.changed", "forecast:current")

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        board: Any = ctx.board
        if post.kind == "forecast":
            diff = belief_diff(board, post)
            return [diff] if diff else []
        body = post.body
        prev = body.get("previous_sha256")
        if body.get("kind") not in ("changed", "removed") or not prev:
            return []
        ref = f"file:{body['path']}@{prev}"
        ids = mark_stale(board, ref)
        board.log_event(NAME, "propagate.stale",
                        {"file": body["path"], "count": len(ids), "ids": ids}, post.id)
        return []

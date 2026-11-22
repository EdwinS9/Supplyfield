"""The executor: the only component allowed to send anything out.

It acts on `action:allowed` and `action:approved` posts. Plain code, no LLM.
The gate adds a "verdict" key to the action body; everything else is
`Action.to_dict()`.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from gtl.contracts import Claim, Context, Evidence, Notice, OutboundMail, Post

NAME = "executor"

COMMITMENTS = ("accept_offer", "place_order", "confirm_terms", "book_freight")
CONTROL_ROOM_KINDS = ("request_pickup", "request_dock_slot", "request_concession",
                      "data_fix_request", "quarantine_batch", "set_priority")
EXECUTABLE = ("allowed", "approved")


# -- kill switch -------------------------------------------------------------
def _seq(post: Post | None) -> int:
    return int(post.id.rsplit("_", 1)[1]) if post is not None and post.id else 0


def is_paused(board: Any) -> bool:
    """True when the newest control.pause event post is newer than the newest control.resume."""
    return _seq(board.latest_event_post("control.pause")) > _seq(board.latest_event_post("control.resume"))


def resume_pending(board: Any, ctx: Context) -> list[Post]:
    """Execute every allowed or approved action. They are pending by definition."""
    if is_paused(board):
        return []
    out: list[Post] = []
    for status in EXECUTABLE:
        for post in board.query(kind="action", status=status):
            out.extend(execute(post, ctx))
    return out


# -- helpers -----------------------------------------------------------------
def _fact_value(board: Any, entity: str, key: str, field: str) -> Any:
    for f in board.facts(entity, key_prefix=key):
        if f.key == key and f.field == field:
            return f.value
    return None


def _num(x: Any) -> str:
    try:
        return f"{float(x):,.0f}"
    except (TypeError, ValueError):
        return str(x)


def _spend_text(params: dict[str, Any]) -> str:
    """The quote with its currency and, for a foreign quote, the EUR value and the rate date."""
    cur = params.get("currency")
    if params.get("amount") is None or cur == "EUR":
        return f"EUR {_num(params.get('cost_eur', 0))}"
    eur = params.get("cost_eur")
    if eur is None:
        return f"{cur or '?'} {_num(params['amount'])} (not priced: {params.get('unpriced_reason')})"
    return f"{cur or '?'} {_num(params['amount'])} (EUR {float(eur):,.2f}, rate of {params.get('fx_date')})"


def _supplier_name(ctx: Context, supplier_id: str) -> str:
    return _fact_value(ctx.board, "supplier", supplier_id, "name") or supplier_id


def _reference(params: dict[str, Any]) -> str:
    return str(params.get("offer_key") or params.get("po_number") or "")


def commitment_text(kind: str, params: dict[str, Any], ctx: Context) -> tuple[str, str]:
    """Plain English subject and body for a commitment the agent gave no text for."""
    ref = _reference(params)
    site = ctx.tenant.get("site", "our site")
    company = ctx.tenant.get("name", "")
    part, qty, price = params.get("part_id", ""), params.get("qty"), params.get("unit_price")
    total = params.get("amount") if params.get("amount") is not None else params.get("cost_eur")
    if total is None and qty is not None and price is not None:
        try:
            total = float(qty) * float(price)
        except (TypeError, ValueError):
            total = None
    cur = params.get("currency") or ("EUR" if params.get("amount") is None else "")
    lines: list[str] = []
    if kind == "confirm_terms":
        subject = f"Confirmation of terms {ref}".strip()
        lines.append(f"We confirm the following term for {ref or 'our order'}: "
                     f"{params.get('term', '')} = {params.get('value', '')}.")
    else:
        verb = "Freight booking" if kind == "book_freight" else "Order"
        subject = f"{verb} {ref}".strip()
        if kind == "book_freight":
            lines.append(f"We book freight from {params.get('from', '')} to {params.get('to', '')} "
                         f"by {params.get('mode', '')}.")
        else:
            lines.append(f"We order from you: {part}.")
        lines.append(f"Part: {part}")
        lines.append(f"Quantity: {_num(qty)}")
        if price is not None:
            lines.append(f"Unit price: {cur} {price}")
        if total is not None:
            lines.append(f"Total: {cur} {_num(total)}")
        if ref:
            lines.append(f"Reference: {ref}")
        lines.append(f"Requested delivery: to {site}, as soon as possible.")
    lines.append("Please confirm by reply.")
    if company:
        lines.append(f"\nRegards,\n{company}")
    return subject, "\n".join(lines)


def _set(board: Any, post: Post, status: str, **extra: Any) -> Post:
    body = dict(post.body)
    body.update(extra)
    board.update_body(post.id, body, NAME)
    return board.set_status(post.id, status, NAME)


def _set_fact_status(board: Any, key_match: Any, want: str, to: str) -> list[str]:
    """Move stock qty fact posts whose key matches from status `want` to `to`."""
    changed: list[str] = []
    for p in board.query(kind="fact", status=want, subject_prefix="stock/"):
        b = p.body
        if b.get("field") != "qty" or not key_match(str(b.get("key", ""))):
            continue
        body = dict(b)
        body["quarantined"] = to == "quarantined"
        board.update_body(p.id, body, NAME)
        board.set_status(p.id, to, NAME, note="executor")
        changed.append(p.id)
    return changed


def _defects_of(board: Any, body: dict[str, Any], params: dict[str, Any]) -> list[Post]:
    """The defects a data fix request refers to: `defect_ids` or the old `defect_id` in the params,
    else the first defect in the context."""
    ids = params.get("defect_ids") or ([params["defect_id"]] if params.get("defect_id") else None)
    posts = [board.get(str(pid)) for pid in (ids or body.get("context_post_ids") or ())]
    found = [p for p in posts if p is not None and p.kind == "defect"]
    return found if ids else found[:1]


def _small(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if k not in ("body", "body_en")}


def _send(ctx: Context, post: Post, params: dict[str, Any], meta: dict[str, Any]) -> str:
    mail = OutboundMail(inbox=params["inbox"], to=params["to"], subject=params["subject"],
                        body=params["body"], thread_id=params.get("thread_id"),
                        idempotency_key=post.body.get("idempotency_key") or "", meta=meta)
    return ctx.ports.mail.send(mail)


def _sent_record(ctx: Context, params: dict[str, Any], message_id: str) -> dict[str, Any]:
    """Snapshot of the outgoing email kept on the action post for admin traffic views."""
    thread_of = getattr(ctx.ports.mail, "thread_of", None)
    thread_id = (thread_of(message_id) if callable(thread_of) and message_id else None) or params.get("thread_id")
    own = {str(a).strip().lower() for a in (ctx.tenant.get("inboxes") or {}).values() if isinstance(a, str)}
    to_addr = str(params.get("to") or "")
    if to_addr.strip().lower() in own:
        route = "internal"
    elif (ctx.tenant.get("mail") or {}).get("outbound") == "live":
        route = "live"
    else:
        route = "stand-in"
    return {"inbox": params.get("inbox"), "to": to_addr, "subject": params.get("subject"),
            "body": params.get("body"), "thread_id": thread_id, "route": route}


def _notice_text(kind: str, params: dict[str, Any], ctx: Context) -> tuple[str, str]:
    if kind in ("accept_offer", "place_order"):
        who = _supplier_name(ctx, str(params.get("supplier_id")))
        title = (f"Ordered {_num(params.get('qty'))} x {params.get('part_id')} from {who} "
                 f"for {_spend_text(params)}")
        return title, f"Reference {_reference(params)}. Sent from the orders inbox; waiting for confirmation."
    if kind == "confirm_terms":
        who = _supplier_name(ctx, str(params.get("supplier_id")))
        return (f"Confirmed {params.get('term')} = {params.get('value')} with {who}",
                f"Reference {_reference(params)}.")
    if kind == "book_freight":
        return (f"Booked freight for {_num(params.get('qty'))} x {params.get('part_id')} "
                f"for EUR {_num(params.get('cost_eur', 0))}",
                f"From {params.get('from')} to {params.get('to')} by {params.get('mode')}.")
    if kind == "quarantine_batch":
        return (f"Quarantined batch {params.get('batch')} of {params.get('part_id')}",
                "The planner will not use this batch until it is released.")
    return "", ""


# -- the work ----------------------------------------------------------------
def execute(post: Post, ctx: Context) -> list[Post]:
    """Carry out one action post. Returns notices and claims to append."""
    board: Any = ctx.board
    body = post.body
    kind, params = body.get("kind"), dict(body.get("params") or {})
    key = body.get("idempotency_key") or ""
    verdict = body.get("verdict") or {}
    approved = post.status == "approved"

    # Defence in depth: the gate must have ruled on this action.
    want = "needs_approval" if approved else "allowed"
    if not isinstance(body.get("verdict"), dict) or body["verdict"].get("outcome") != want:
        _set(board, post, "failed", error="no gate verdict")
        return []

    if key:
        for other in board.query(kind="action", status="executed"):
            if other.id != post.id and other.body.get("idempotency_key") == key:
                _set(board, post, "executed", duplicate_of=other.id)
                return []

    out: list[Post] = []
    try:
        extra: dict[str, Any] = {}
        if kind == "send_email":
            mid = _send(ctx, post, params, {
                "action_kind": "send_email", "action_id": post.id, "params": _small(params)})
            extra["message_id"] = mid
            extra["sent_mail"] = _sent_record(ctx, params, mid)
        elif kind in COMMITMENTS:
            if kind == "book_freight":
                to = params.get("to")
            else:
                to = _fact_value(board, "supplier", str(params.get("supplier_id")), "contact_email")
                if not to:
                    raise ValueError(f"no contact_email fact for supplier {params.get('supplier_id')}")
            if to:
                subject, text = commitment_text(kind, params, ctx)
                mail_params = {"inbox": ctx.tenant["inboxes"]["orders"], "to": to,
                               "subject": params.get("subject") or subject,
                               "body": params.get("body") or text,
                               "thread_id": params.get("thread_id")}
                mid = _send(ctx, post, mail_params, {
                    "action_kind": kind, "action_id": post.id, "params": params})
                extra["message_id"] = mid
                extra["sent_mail"] = _sent_record(ctx, mail_params, mid)
            else:
                ctx.ports.control_room.publish(post)
            if params.get("ready_hours") is not None and params.get("transit_hours") is not None:
                hours = float(params["ready_hours"]) + float(params["transit_hours"])
                arrives = ctx.ports.clock.now() + timedelta(hours=hours)
                extra["params"] = {**params, "arrives_at": arrives.isoformat()}
        elif kind in CONTROL_ROOM_KINDS:
            ctx.ports.control_room.publish(post)
            if kind == "data_fix_request":
                for defect in _defects_of(board, body, params):
                    if defect.status == "open":
                        board.set_status(defect.id, "requested", NAME, f"asked {params.get('owner_role', 'the owner')}")
        else:
            raise ValueError(f"unknown action kind {kind!r}")
        extra["executed_at"] = ctx.ports.clock.now().isoformat()

        # Effects on what the warehouse may use.
        if kind == "quarantine_batch":
            want = "|".join(str(params.get(k)) for k in ("site", "part_id", "batch"))
            _set_fact_status(board, lambda k: k == want, "active", "quarantined")
        elif kind == "request_concession" and approved:
            gr = str(params.get("gr_number"))
            _set_fact_status(board, lambda k: k.split("|")[-1] == gr, "quarantined", "active")
            who = verdict.get("approver_person") or verdict.get("approver_role") or "approver"
            claim = Claim(entity="inspection", key=gr, field="concession", value=True,
                          as_of=ctx.ports.clock.now().isoformat(), source_kind="human",
                          evidence=(Evidence(source=f"human:{who}"),), author=NAME)
            out.append(Post(kind="claim", status="proposed", subject=f"inspection/{gr}/concession",
                            body=claim.to_dict(), author=NAME, depends_on=(post.id,)))
        _set(board, post, "executed", **extra)
    except Exception as exc:  # noqa: BLE001 - a port failure marks the action, it does not stop the cycle
        _set(board, post, "failed", error=f"{type(exc).__name__}: {exc}")
        return []

    if not approved:
        title, detail = _notice_text(kind, params, ctx)
        if title:
            n = Notice(title=title, detail=detail, action_id=post.id, rule_id=verdict.get("rule_id"))
            out.append(Post(kind="notice", status="posted", subject=title, body=n.to_dict(),
                            author=NAME, depends_on=(post.id,)))
    return out


class ExecutorAgent:
    name = NAME
    subscribes = ("action:allowed", "action:approved", "event:control.resume")

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        board: Any = ctx.board
        if is_paused(board):
            return []  # actions stay allowed or approved and run on resume
        if post.kind == "event":
            return resume_pending(board, ctx)
        cur = board.get(post.id)
        if cur is None or cur.status not in EXECUTABLE:
            return []
        return execute(cur, ctx)

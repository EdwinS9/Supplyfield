"""Asking a supplier for a better price when our records show the mistake was theirs.

The liaison only asks. The request rides in the mail that clears up the mistake, so no extra mail
goes out. Every step is logged as a `supply.negotiation` event: the request, each reminder, the
supplier's reply with the terms it names, or the fact that nobody answered. Accepting terms is a
commitment and stays with a person: the reply goes to the buyer as a question.

Settings, in the tenant file under `[negotiation]`: `enabled` (default true) and `role` (who
decides on a reply, default "buyer").
"""
from __future__ import annotations

from typing import Any

from gtl.contracts import Post, Question

NEGOTIATION_EVENT = "supply.negotiation"  # payload: {"stage": asked|reminded|replied|no_answer, "question_ids", "to_id", ...}
DEFAULT_ROLE = "buyer"
PRICE_FIELDS = ("unit_price", "surcharge_pct", "currency")
_TERM_WORDS = ("price", "discount", "credit", "rebate", "reduction", "refund", "compensat")

MAIL_RULE = (
    "Our records show a mistake on the recipient's side, listed under 'Their mistake'. After the "
    "questions, add one more numbered point: name the mistake in one neutral sentence and ask what "
    "price reduction or credit note they offer on the affected order for the extra work and risk it "
    "caused us. Name no amount, make no threat, accept nothing. "
)
READ_HINT = (
    "\nOur mail also asked for a price reduction or a credit note because of a mistake on their side. "
    "Extract what the mail answers to that: a new price as an offer or order_confirmation claim, and "
    "any other answer to it (a credit note, a percentage, a refusal) as an observation."
)


def enabled(tenant: dict[str, Any]) -> bool:
    return (tenant.get("negotiation") or {}).get("enabled", True) is not False


def decider(tenant: dict[str, Any]) -> str:
    return str((tenant.get("negotiation") or {}).get("role") or DEFAULT_ROLE)


def faults(board: Any, supplier_id: str, question_posts: list[Post]) -> list[dict[str, Any]]:
    """The supplier's own mistakes behind these questions.

    A mistake counts when the data steward raised a defect that this supplier owns (the wrong value
    came from the supplier's document or mail) for a conflict one of the questions is about.
    """
    asked_for: dict[str, str] = {}
    for qp in question_posts:
        for post_id in qp.body.get("context_post_ids") or ():
            asked_for.setdefault(str(post_id), qp.id)
    if not asked_for:
        return []
    found = []
    for d in board.query(kind="defect", limit=100_000):
        b = d.body
        if d.status == "dismissed" or b.get("owner_supplier") != supplier_id or b.get("conflict_id") not in asked_for:
            continue
        found.append({"defect_id": d.id, "question_id": asked_for[b["conflict_id"]], "entity": b.get("entity"),
                      "key": b.get("key"), "detail": b.get("detail") or "",
                      "copies_at_risk": int(b.get("copies_at_risk") or 0)})
    return found


def fallback_line(found: list[dict[str, Any]]) -> str:
    """The request in plain English, for when the model is down."""
    what = " ".join(f["detail"] for f in found if f["detail"]) or "see the points above."
    return (f"Our records show a mistake on your side: {what} "
            "What price reduction or credit note do you offer on the affected order?")


def asked_in(board: Any, question_posts: list[Post]) -> dict[str, Any] | None:
    """The request made in the newest mail that asked these questions, or None when none was made."""
    wanted = {p.id for p in question_posts}
    for action in reversed(board.query(kind="action", limit=100_000)):
        params = action.body.get("params") or {}
        if action.body.get("kind") != "send_email" or not isinstance(params.get("negotiation"), dict):
            continue
        ids = params.get("question_ids") or [params.get("question_id")]
        if wanted & {str(i) for i in ids if i}:
            return {**params["negotiation"], "action_id": action.id, "to_id": params.get("to_id")}
    return None


def terms(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a reply says about price: price fields, and remarks that name a price, discount or credit."""
    out = []
    for c in claims:
        value = c.get("value")
        priced = c.get("field") in PRICE_FIELDS
        remark = c.get("entity") == "observation" and isinstance(value, str) \
            and any(word in value.lower() for word in _TERM_WORDS)
        if not (priced or remark):
            continue
        quote = next((e.get("quote") for e in c.get("evidence") or () if e.get("quote")), None)
        out.append({"entity": c.get("entity"), "key": c.get("key"), "field": c.get("field"), "value": value,
                    "unit": c.get("unit"), "quote": quote})
    return out


def terms_text(found: list[dict[str, Any]]) -> str:
    if not found:
        return "The reply names no price reduction or credit."
    lines = []
    for t in found:
        said = f"\"{t['quote']}\"" if t.get("quote") else str(t.get("value"))
        lines.append(said if t.get("entity") == "observation" else f"{t['field']} {t['value']} ({said})")
    return "The reply says: " + "; ".join(lines) + "."


def log(board: Any, actor: str, stage: str, question_ids: list[str], to_id: Any, **more: Any) -> None:
    """One step of a negotiation on record, against the first question it belongs to."""
    board.log_event(actor, NEGOTIATION_EVENT, {"stage": stage, "question_ids": question_ids, "to_id": to_id, **more},
                    question_ids[0] if question_ids else None)


def decision_question(tenant: dict[str, Any], supplier_name: str, to_id: str, topic: str, text: str,
                      context: tuple[str, ...], author: str) -> Post:
    """The reply to our price request, handed to the person who may accept it."""
    q = Question(
        text=(f"{supplier_name} replied to our request for a price reduction after a mistake on their side. {text} "
              "Please decide whether to accept, ask for more, or drop it. The system accepts nothing on its own."),
        to_kind="role", to_id=decider(tenant), topic=f"supplier:price-reply:{to_id}:{topic}",
        channel="supply", context_post_ids=context)
    return Post(kind="question", status="open", subject=f"{q.to_id}|{q.topic}", body=q.to_dict(), author=author,
                depends_on=context)

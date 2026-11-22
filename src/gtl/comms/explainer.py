"""The explainer: says in plain words why the rules allowed a step, sent it to a person, or stopped it.

The gate records a verdict and a rule id. A person reading the trail wants the reason: which
sentence of the policy applies, which numbers of this step meet it, whose authority covers it,
and why it went to the person it went to. This agent writes that down once per ruling, as an
`action.explained` event on the action's post, from the policy files and the facts on the Board.

Every sentence is built from data. With `[explainer] use_model = true` in the tenant file the
model may reword the sentences; it gets only those sentences and the template stays the fallback.
The model is used in the real world only: in a simulation nobody reads the text, so Gym runs cost
the same with or without the setting.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from gtl.contracts import Action, Context, Post
from gtl.govern.policy import Policy, action_category, first_filled, load_policy, resolve_role

NAME = "explainer"
EXPLAIN_EVENT = "action.explained"  # payload: {"outcome", "rule_id", "sentences": [...], "written_by": "template"|"model"}
_OPS = {"<": "below", "<=": "up to", ">": "above", ">=": "of at least", "==": "of exactly"}
_STEP_WORDS = {
    "quarantine_batch": "holding a batch back from use", "data_fix_request": "asking a record owner to check a record",
    "set_priority": "choosing which product gets shared parts first", "confirm_terms": "confirming delivery terms",
    "send_email": "sending a question by email",
}
REWORD_SYSTEM = (
    "You explain a procurement decision to a manager. Rewrite the given sentences as two to four short, "
    "plain sentences. Use only what the sentences say: no new numbers, names, rules or reasons. Keep every "
    "amount, name and policy section exactly. Answer with JSON only: {\"sentences\": [\"...\"]}."
)
REWORD_SCHEMA: dict[str, Any] = {
    "type": "object", "properties": {"sentences": {"type": "array", "items": {"type": "string"}}},
    "required": ["sentences"],
}


def _money(value: Any) -> str:
    return f"EUR {float(value):,.0f}"


def _who(policy: Policy, role_id: str) -> str:
    role = policy.roles.get(role_id)
    if role is None:
        return role_id.replace("_", " ")
    return f"{role.person} ({role.title})" if role.person else role.title


def _conditions(rule: Any, body: dict[str, Any]) -> str:
    """The numbers of this step that meet the rule, in words."""
    parts = []
    for cond in rule.when:
        field, op, value = str(cond.get("field")), str(cond.get("op")), cond.get("value")
        if field == "cost_eur":
            parts.append(f"the rule covers amounts {_OPS.get(op, op)} {_money(value)}")
        elif field.startswith("params."):
            parts.append(f"{field[7:].replace('_', ' ')} is {(body.get('params') or {}).get(field[7:])}")
    if not parts:
        return ""
    lead = f"This step costs {_money(body.get('cost_eur') or 0)}, and " if any("amounts" in p for p in parts) else "Here "
    return lead + " and ".join(parts) + "."


def _quote(rule: Any) -> str:
    section = f"section {rule.source_section}" if rule.source_section and rule.source_section != "default" else ""
    quote = str(rule.quote or "").strip()
    if section and quote:
        return f"Policy {section} says: \"{quote}\"" + ("" if quote[-1] in ".!?" else ".")
    return f"Policy {section} applies." if section else ""


def explain(board: Any, tenant: dict[str, Any], policy: Policy | None, post: Post, now: datetime | None = None) -> list[str]:
    """Plain sentences on the gate's ruling for one action. Empty when the gate has not ruled yet."""
    body = post.body
    verdict = body.get("verdict") or {}
    outcome, reason = verdict.get("outcome"), str(verdict.get("reason") or "")
    if not outcome:
        return []
    kind = str(body.get("kind"))
    params = body.get("params") or {}
    if outcome == "blocked":
        return [f"The rules stopped this step: {reason}.", "Nothing was sent or ordered."]
    if kind == "send_email" and outcome == "allowed":
        return ["No approval was needed. An email may go out on its own when it is sent from one of our own "
                "inboxes to the contact address on record for that supplier.",
                f"This one went from {params.get('inbox')} to {params.get('to')}, which is that address.",
                "It only asks a question. It orders nothing and promises nothing."]
    rule = next((r for r in policy.rules if r.id == verdict.get("rule_id")), None) if policy is not None else None
    if rule is None:
        lead = "No person had to approve this." if outcome == "allowed" else "This needed a person."
        return [lead, f"Reason given by the rules: {reason}."]
    assert policy is not None
    action = Action(**{k: v for k, v in body.items() if k in Action.__dataclass_fields__})
    alias = str((tenant.get("autonomy") or {}).get("resolver_role") or "buyer")
    mine, _ = first_filled(policy, resolve_role(policy, alias, action, board), now)
    category = action_category(action, board)
    out: list[str] = []
    if outcome == "allowed":
        if not rule.approver_roles:
            out.append(f"No approval was needed. The procurement policy names no approver for "
                       f"{_STEP_WORDS.get(kind, kind.replace('_', ' '))}.")
            if not float(body.get("cost_eur") or 0):
                out.append("It spends no money and places no order.")
            return out
        out.append("No person had to approve this.")
        out += [s for s in (_quote(rule), _conditions(rule, body)) if s]
        owner = f"For the category {category}, that " if category else "That "
        out.append(f"{owner}authority belongs to {_who(policy, mine)}.")
        out.append("The agents are set up to act with exactly that authority, so the step could go ahead on its own.")
        return out
    out.append("This needed a person.")
    out += [s for s in (_quote(rule), _conditions(rule, body)) if s]
    approvers = body.get("approvers") or [{"role": verdict.get("approver_role"), "routed_from": None}]
    for a in approvers:
        role = str(a.get("role"))
        if a.get("routed_from"):
            seat = policy.roles.get(str(a["routed_from"]))
            until = f" until {seat.valid_from}" if seat is not None and seat.valid_from and not seat.vacant else ""
            title = seat.title if seat is not None else str(a["routed_from"]).replace("_", " ")
            out.append(f"The seat of {title} is empty{until}, so the request went up to {_who(policy, role)}.")
        elif role == mine:
            out.append(f"Money is committed in the real world here, and for now every such step needs one click "
                       f"from {_who(policy, role)}.")
        else:
            out.append(f"{_who(policy, role)} has to approve.")
    if not any(a.get("role") == mine for a in approvers):
        own = f"The agents act with the authority of {_who(policy, mine)}, which" if category else "The agents' own authority"
        out.append(f"{own} does not cover this step.")
    if "add up to" in reason or "not priced" in reason or str(verdict.get("rule_id", "")).startswith("guardrail"):
        out.append(f"Also noted by the rules: {reason}.")
    return out


def _reword(ctx: Context, sentences: list[str]) -> list[str] | None:
    """The same content in the model's words, or None when the model fails or drifts."""
    try:
        data = ctx.llm.complete("agent", REWORD_SYSTEM, "\n".join(sentences), schema=REWORD_SCHEMA).data
    except Exception:  # noqa: BLE001 - the template is always good enough
        return None
    found = data.get("sentences") if isinstance(data, dict) else None
    if not isinstance(found, list) or not 1 <= len(found) <= 6:
        return None
    clean = [s.strip() for s in found if isinstance(s, str) and s.strip()]
    return clean if len(clean) == len(found) else None


class ExplainerAgent:
    name = NAME
    subscribes = ("action:allowed", "action:needs_approval", "action:blocked")

    def __init__(self, root: str = "tenants") -> None:
        self._root = root
        self._policy: Policy | None = None

    def _get_policy(self, ctx: Context) -> Policy | None:
        tenant_id = ctx.tenant.get("id")
        if self._policy is None or self._policy.tenant_id != tenant_id:
            try:
                self._policy = load_policy(str(tenant_id), self._root)
            except (OSError, ValueError, KeyError):
                self._policy = None
        return self._policy

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        try:
            sentences = explain(ctx.board, ctx.tenant, self._get_policy(ctx), post, ctx.ports.clock.now())
        except Exception as exc:  # noqa: BLE001 - a missing explanation never stops an action
            ctx.board.log_event(NAME, "explainer.failed", {"reason": f"{type(exc).__name__}: {exc}"}, post.id)
            return []
        if not sentences:
            return []
        written_by = "template"
        if (ctx.tenant.get("explainer") or {}).get("use_model") is True and ctx.world == "real":
            reworded = _reword(ctx, sentences)
            if reworded:
                sentences, written_by = reworded, "model"
        verdict = post.body.get("verdict") or {}
        ctx.board.log_event(NAME, EXPLAIN_EVENT, {"outcome": verdict.get("outcome"), "rule_id": verdict.get("rule_id"),
                                                  "sentences": sentences, "written_by": written_by}, post.id)
        return []

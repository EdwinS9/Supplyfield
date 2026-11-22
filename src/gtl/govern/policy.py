"""The customer's approval policy as data, and the rules that turn it into verdicts.

The policy and the org chart live in `tenants/<id>/generated/` (policy.json and
org.json). Nothing in this module names a customer, a role or a supplier.
"""
from __future__ import annotations

import hashlib
import json
import operator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from gtl.config import supplier_directory
from gtl.contracts import Action, Claim, Evidence, GateVerdict, Post

COMMITMENTS = ("accept_offer", "place_order", "book_freight")
_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
        "==": operator.eq, "in": lambda a, b: a in b}
# Statuses of earlier action posts that no longer count as spend or stock of orders.
_DEAD_STATUSES = ("rejected", "blocked", "failed")


@dataclass(frozen=True)
class Rule:
    id: str
    actions: tuple[str, ...]
    when: tuple[dict[str, Any], ...]
    approver_roles: tuple[str, ...]
    source_section: str
    quote: str


@dataclass(frozen=True)
class Role:
    id: str
    title: str
    person: str | None
    vacant: bool
    reports_to: str | None
    categories: tuple[str, ...] = ()
    function: str | None = None  # "buyer" or "category_lead": what an alias looks for
    deputy: str | None = None
    valid_from: str | None = None  # ISO date the holder starts; the seat counts as vacant before it

    def vacant_at(self, now: datetime | None) -> bool:
        """Vacant, or filled by someone who has not started yet."""
        if self.vacant:
            return True
        if not self.valid_from or now is None:
            return False
        try:
            return now.date() < date.fromisoformat(self.valid_from)
        except ValueError:
            return False


@dataclass
class Policy:
    tenant_id: str
    rules: list[Rule]
    roles: dict[str, Role]
    record_owners: dict[str, dict[str, Any]]
    aliases: dict[str, dict[str, Any]]
    files: dict[str, Path] = field(default_factory=dict)  # "policy" and "org" -> path

    @property
    def release_days(self) -> int:
        """The conditional-release limit, read from the extension rule."""
        for rule in self.rules:
            for cond in rule.when:
                if cond["field"] == "release_days":
                    return int(cond["value"])
        return 90


def _root_path(root: str | Path) -> Path:
    p = Path(root)
    if p.is_absolute() or p.exists():
        return p
    return Path(__file__).resolve().parents[3] / p


def load_policy(tenant_id: str, root: str | Path = "tenants") -> Policy:
    base = _root_path(root) / tenant_id / "generated"
    org = json.loads((base / "org.json").read_text(encoding="utf-8"))
    pol = json.loads((base / "policy.json").read_text(encoding="utf-8"))
    roles = {}
    for r in org["roles"]:
        roles[r["id"]] = Role(id=r["id"], title=r["title"], person=r.get("person"),
                              vacant=bool(r.get("vacant")), reports_to=r.get("reports_to"),
                              categories=tuple(r.get("categories", ())), function=r.get("function"),
                              deputy=r.get("deputy"), valid_from=r.get("valid_from"))
    rules = [Rule(id=r["id"], actions=tuple(r["actions"]), when=tuple(r.get("when", ())),
                  approver_roles=tuple(r["approver_roles"]), source_section=r.get("source_section", ""),
                  quote=r.get("quote", "")) for r in pol["rules"]]
    return Policy(tenant_id=tenant_id, rules=rules, roles=roles,
                  record_owners=dict(org.get("record_owners", {})), aliases=dict(pol.get("aliases", {})),
                  files={"policy": base / "policy.json", "org": base / "org.json"})


# ---------------------------------------------------------------------------
# Claims for the Board
# ---------------------------------------------------------------------------
def _condition_text(rule: Rule) -> str:
    return " and ".join(f"{c['field']} {c['op']} {c['value']}" for c in rule.when)


def config_claims(policy: Policy, now_iso: str) -> list[Claim]:
    """Role, approval_rule and record_owner claims so the policy becomes facts on the Board."""
    def evidence(kind: str, quote: str) -> tuple[Evidence, ...]:
        path = policy.files[kind]
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        return (Evidence(source=f"tenants/{policy.tenant_id}/generated/{path.name}", sha256=sha, quote=quote),)

    def claim(entity: str, key: str, fld: str, value: Any, ev: tuple[Evidence, ...]) -> Claim:
        return Claim(entity=entity, key=key, field=fld, value=value, as_of=now_iso,
                     source_kind="config", evidence=ev, author="gate")

    out: list[Claim] = []
    for role in policy.roles.values():
        ev = evidence("org", f"{role.title}: {role.person or 'TBD'}")
        out += [claim("role", role.id, "title", role.title, ev),
                claim("role", role.id, "person", role.person or "", ev),
                claim("role", role.id, "vacant", role.vacant, ev),
                claim("role", role.id, "reports_to", role.reports_to or "", ev)]
        if role.valid_from:
            out.append(claim("role", role.id, "valid_from", role.valid_from, ev))
    for rule in policy.rules:
        ev = evidence("policy", rule.quote)
        out += [claim("approval_rule", rule.id, "action", list(rule.actions), ev),
                claim("approval_rule", rule.id, "condition", _condition_text(rule), ev),
                claim("approval_rule", rule.id, "approver_roles", list(rule.approver_roles), ev),
                claim("approval_rule", rule.id, "source_section", rule.source_section, ev)]
    for entity, owner in policy.record_owners.items():
        ev = evidence("org", f"record owner of {entity}: {owner['role_id']}")
        out += [claim("record_owner", entity, "role_id", owner["role_id"], ev),
                claim("record_owner", entity, "corroborate_with", list(owner.get("corroborate_with", [])), ev)]
    return out


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
def _fact_value(board: Any, entity: str, key: str, fld: str) -> Any:
    for f in board.facts(entity, key):
        if f.key == key and f.field == fld:
            return f.value
    return None


def action_category(action: Action, board: Any) -> str | None:
    cat = action.params.get("category")
    if cat:
        return str(cat)
    part_id = action.params.get("part_id")
    if part_id:
        val = _fact_value(board, "part", str(part_id), "category")
        return str(val) if val else None
    return None


def resolve_role(policy: Policy, role_or_alias: str, action: Action, board: Any) -> str:
    """Turn an alias such as "buyer" into a concrete role id for this action."""
    alias = policy.aliases.get(role_or_alias)
    if alias is None:
        return role_or_alias
    if "role" in alias:
        return str(alias["role"])
    category = action_category(action, board)
    if category is not None:
        for role in policy.roles.values():
            if role.function == alias.get("function") and category in role.categories:
                return role.id
    return resolve_role(policy, alias["fallback"], action, board) if alias.get("fallback") \
        else "head_of_procurement"


def first_filled(policy: Policy, role_id: str, now: datetime | None = None) -> tuple[str, bool]:
    """A vacant role routes up the reports_to chain to the first filled role.

    With `now`, a seat whose holder starts later is vacant until that day.
    """
    routed = False
    seen: set[str] = set()
    current = role_id
    while current in policy.roles and policy.roles[current].vacant_at(now) and current not in seen:
        seen.add(current)
        up = policy.roles[current].reports_to
        if not up:
            break
        current, routed = up, True
    return current, routed


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------
def _lookup(env: dict[str, Any], name: str) -> Any:
    if name.startswith("params."):
        return env["params"].get(name[len("params."):])
    return env.get(name)


def _holds(cond: dict[str, Any], env: dict[str, Any]) -> bool:
    actual = _lookup(env, cond["field"])
    if actual is None:
        return False
    try:
        return bool(_OPS[cond["op"]](actual, cond["value"]))
    except TypeError:
        return False


def _specificity(rule: Rule) -> tuple[int, int]:
    on_params = sum(1 for c in rule.when if c["field"].startswith("params."))
    return (on_params, len(rule.when))


def matching_rule(policy: Policy, action: Action, env: dict[str, Any]) -> Rule | None:
    hits = [r for r in policy.rules
            if action.kind in r.actions and all(_holds(c, env) for c in r.when)]
    return max(hits, key=_specificity) if hits else None


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _other_actions(board: Any, action: Action, exclude_post_id: str | None) -> list[Any]:
    out = []
    for p in board.query(kind="action"):
        if p.id == exclude_post_id or p.status in _DEAD_STATUSES:
            continue
        if p.body.get("idempotency_key") == action.idempotency_key:
            continue
        out.append(p)
    return out


def _aggregated_cost(action: Action, others: list[Any], tenant: dict[str, Any], now: datetime) -> float:
    """Own cost plus other recent orders for the same supplier and part (no order splitting)."""
    window = float(tenant.get("autonomy", {}).get("split_window_hours", 0))
    supplier, part = action.params.get("supplier_id"), action.params.get("part_id")
    total = float(action.cost_eur)
    if not window or not supplier or not part:
        return total
    since = now - timedelta(hours=window)
    for p in others:
        b = p.body
        if b.get("kind") not in COMMITMENTS:
            continue
        params = b.get("params", {})
        created = _parse(p.created_at)
        if params.get("supplier_id") == supplier and params.get("part_id") == part \
                and created is not None and created >= since:
            total += float(b.get("cost_eur", 0.0))
    return total


def _monthly_air(action: Action, others: list[Any], board: Any, now: datetime) -> float:
    total = float(action.cost_eur)
    category = action_category(action, board)
    for p in others:
        b = p.body
        if b.get("kind") != "book_freight" or b.get("params", {}).get("mode") != "air":
            continue
        created = _parse(p.created_at)
        if created is None or (created.year, created.month) != (now.year, now.month):
            continue
        other = Action(**{k: v for k, v in b.items() if k in Action.__dataclass_fields__})
        if action_category(other, board) == category:
            total += float(b.get("cost_eur", 0.0))
    return total


def _executed_today(others: list[Any], now: datetime) -> float:
    total = 0.0
    for p in others:
        if p.status != "executed" or p.body.get("kind") not in COMMITMENTS:
            continue
        done = _parse(p.updated_at)
        if done is not None and done.date() == now.date():
            total += float(p.body.get("cost_eur", 0.0))
    return total


def _release_problem(policy: Policy, action: Action, board: Any, now: datetime) -> str | None:
    supplier = action.params.get("supplier_id")
    if not supplier:
        return None
    status = _fact_value(board, "supplier", str(supplier), "release_status")
    if status == "qualified":
        return None
    if status != "conditional":
        if not status:
            return f"release status of supplier {supplier} is unknown; no fact yet"
        return f"supplier {supplier} has no valid release (status: {status}); only released suppliers may be ordered from"
    released = _fact_value(board, "supplier", str(supplier), "release_date")
    try:
        since = date.fromisoformat(str(released)[:10])
    except ValueError:
        return f"supplier {supplier} has a conditional release without a known date"
    age = (now.date() - since).days
    if age > policy.release_days:
        return (f"the conditional release of supplier {supplier} is {age} days old and expired after "
                f"{policy.release_days} days (PP-07 5)")
    return None


def _person(policy: Policy, role_id: str) -> str | None:
    role = policy.roles.get(role_id)
    return role.person if role else None


def evaluate(policy: Policy, action: Action, *, agent_role_alias: str, board: Any,
             tenant: dict[str, Any], now: datetime, world: str = "sim",
             exclude_post_id: str | None = None) -> GateVerdict:
    return assess(policy, action, agent_role_alias=agent_role_alias, board=board, tenant=tenant,
                  now=now, world=world, exclude_post_id=exclude_post_id)[0]


def assess(policy: Policy, action: Action, *, agent_role_alias: str, board: Any,
           tenant: dict[str, Any], now: datetime, world: str = "sim",
           exclude_post_id: str | None = None) -> tuple[GateVerdict, list[dict[str, Any]]]:
    """The verdict plus every approver still needed, in order.

    Each approver is {"role", "person", "rule_id", "routed_from", "source_section"}.
    """
    autonomy = tenant.get("autonomy", {})
    others = _other_actions(board, action, exclude_post_id)
    commitment = action.kind in COMMITMENTS

    # Special case: mail goes out only from our inboxes to a known supplier contact.
    if action.kind == "send_email":
        return _email_verdict(policy, action, board, tenant), []

    unpriced = str(action.params.get("unpriced_reason") or "")
    if unpriced:
        # Never passes a spend rule on its own: judged as the strictest spend tier.
        cost = float("inf")
    else:
        cost = _aggregated_cost(action, others, tenant, now) if commitment else float(action.cost_eur)
    env = {"cost_eur": cost, "params": action.params,
           "monthly_air_eur": _monthly_air(action, others, board, now) if commitment else 0.0}
    rule = matching_rule(policy, action, env)
    if rule is None:
        return GateVerdict("blocked", None, f"no approval rule covers the action kind {action.kind}"), []

    # (role alias, rule id, note) in the order they are required.
    required: list[tuple[str, str, str]] = []
    if commitment and autonomy.get("released_suppliers_only"):
        problem = _release_problem(policy, action, board, now)
        if problem:
            return GateVerdict("blocked", "guardrail_released_suppliers", problem), []
    for alias in rule.approver_roles:
        required.append((alias, rule.id, rule.source_section))
    if commitment:
        if autonomy.get("contract_prices_only") and float(action.params.get("surcharge_pct") or 0) > 0:
            required.append(("category_lead", "guardrail_contract_prices",
                             "a surcharge is not covered by the contract price"))
        cap = autonomy.get("daily_spend_cap_eur")
        if cap is not None and not unpriced and _executed_today(others, now) + float(action.cost_eur) > float(cap):
            required.append(("head_of_procurement", "guardrail_daily_cap",
                             f"the daily spend cap of {cap} EUR would be exceeded"))

    mine, _ = first_filled(policy, resolve_role(policy, agent_role_alias, action, board), now)
    approvers: list[dict[str, Any]] = []
    notes: dict[str, str] = {}
    for alias, rule_id, note in required:
        wanted = resolve_role(policy, alias, action, board)
        final, routed = first_filled(policy, wanted, now)
        if final not in [a["role"] for a in approvers]:
            approvers.append({"role": final, "person": _person(policy, final), "rule_id": rule_id,
                              "routed_from": wanted if routed else None,
                              "source_section": rule.source_section})
            notes[final] = note
    pending = [a for a in approvers if a["role"] != mine]
    if not pending and unpriced:
        one = {"role": mine, "person": _person(policy, mine), "rule_id": rule.id,
               "routed_from": None, "source_section": rule.source_section}
        return (GateVerdict("needs_approval", rule.id, f"not priced: {unpriced}",
                            approver_role=mine, approver_person=_person(policy, mine)), [one])
    if not pending:
        if commitment and world == "real" and autonomy.get("live_commit_mode") == "approve":
            one = {"role": mine, "person": _person(policy, mine), "rule_id": rule.id,
                   "routed_from": None, "source_section": rule.source_section}
            return (GateVerdict("needs_approval", rule.id,
                                "live commitments need one click until the playbooks graduate",
                                approver_role=mine, approver_person=_person(policy, mine)), [one])
        return GateVerdict("allowed", rule.id, f"within the authority of {mine} ({rule.source_section})"), []

    first = pending[0]
    parts = [f"{rule.source_section}: needs approval by " + " and ".join(a["role"] for a in approvers)]
    if unpriced:
        parts.insert(0, f"not priced: {unpriced}")
    elif cost != float(action.cost_eur):
        parts.append(f"orders for the same supplier and part add up to {cost:g} EUR")
    parts += [f"{a['routed_from']} is vacant, routed to {a['role']} ({rule.source_section})"
              for a in approvers if a["routed_from"]]
    if first["rule_id"] != rule.id:
        parts.append(notes[first["role"]])
    return (GateVerdict("needs_approval", first["rule_id"], "; ".join(parts),
                        approver_role=first["role"], approver_person=first["person"]), pending)


def _email_verdict(policy: Policy, action: Action, board: Any, tenant: dict[str, Any]) -> GateVerdict:
    rule = matching_rule(policy, action, {"cost_eur": 0.0, "params": action.params})
    rule_id = rule.id if rule else None
    inbox = action.params.get("inbox")
    if inbox not in tenant.get("inboxes", {}).values():
        return GateVerdict("blocked", rule_id, f"{inbox} is not one of our inboxes")
    to = str(action.params.get("to", "")).strip().lower()
    known = {str(f.value).strip().lower() for f in board.facts("supplier") if f.field == "contact_email"}
    lead = supplier_directory(tenant).get(action.params.get("to_id"), {})
    if to and to == str(lead.get("contact_email", "")).strip().lower() and str(
            action.params.get("topic", "")).startswith(("spare:", "recovery:")):
        return GateVerdict("allowed", rule_id, "quote request to the tenant's sourcing directory contact")
    if to not in known:
        return GateVerdict("blocked", rule_id, f"{to or 'no recipient'} is not a known supplier contact")
    return GateVerdict("allowed", rule_id, "question from our inbox to a known supplier contact")


def seed_config_claims(ctx: Any) -> int:
    """Append the policy and org chart as proposed claims, once. Returns how many were added."""
    board = ctx.board
    if board.facts("role") or board.query(kind="claim", subject_prefix="role/"):
        return 0
    policy = load_policy(ctx.tenant["id"])
    claims = config_claims(policy, ctx.ports.clock.now().isoformat())
    for c in claims:
        board.append(Post(kind="claim", status="proposed", subject=f"{c.entity}/{c.key}/{c.field}",
                          body=c.to_dict(), author="gate"))
    return len(claims)

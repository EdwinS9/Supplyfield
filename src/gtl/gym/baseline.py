"""The scripted baseline (docs/gym.md section 8): no LLM, no pricing, the reference line on the learning curve."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from gtl import registry
from gtl.contracts import Context, Post
from gtl.govern.policy import Policy, load_policy
from gtl.plan import build_plan_input, plan
from gtl.resolve.candidates import assume_pending, candidates

NAME = "baseline"
RESOLVER = "resolver"


def buyer_limit(policy: Policy, role: str) -> float:
    """The spend below which the resolver's role may approve alone: the upper bound of its spend rule."""
    bounds = [float(c["value"]) for r in policy.rules if r.approver_roles == (role,) and "accept_offer" in r.actions
              for c in r.when if c["field"] == "cost_eur" and c["op"] == "<"]
    return min(bounds, default=0.0)


class BaselineAgent:
    """Requests every candidate fix that is free or under the buyer limit. Escalates nothing else."""

    name = NAME
    subscribes = ("forecast:current",)

    def __init__(self, policy: Policy | None = None) -> None:
        self._policy = policy

    def _limit(self, ctx: Context) -> float:
        if self._policy is None or self._policy.tenant_id != ctx.tenant["id"]:
            self._policy = load_policy(ctx.tenant["id"])
        return buyer_limit(self._policy, ctx.tenant["autonomy"]["resolver_role"])

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        now = ctx.ports.clock.now()
        pi = assume_pending(ctx.board, ctx.tenant, now, build_plan_input(ctx.board, ctx.tenant, now))
        limit = self._limit(ctx)
        out: list[Post] = []
        for cand in candidates(ctx.board, ctx.tenant, now, plan(pi), pi):
            if cand.cost_eur > 0 and cand.cost_eur >= limit:
                continue
            for action in cand.actions:
                act = replace(action, requested_by=NAME, reason=cand.title)
                out.append(Post(kind="action", status="requested", subject=act.idempotency_key,
                                body=act.to_dict(), author=NAME, depends_on=(post.id,)))
        return out


def baseline_agents() -> list[Any]:
    """The registry agents with the resolver replaced by the baseline."""
    return [BaselineAgent() if a.name == RESOLVER else a for a in registry.load_agents()]

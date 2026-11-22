"""The planner agent: a thin wrapper that posts one current forecast."""
from __future__ import annotations

from dataclasses import replace

from gtl.contracts import Context, Post
from gtl.plan.inputs import build_plan_input
from gtl.plan.solver import plan

# Entities that feed the plan input; their active fact posts are the evidence.
_USED = ("product", "bom", "line", "shift", "part", "stock", "shipment", "route", "po_line")


def _number(post_id: str) -> int:
    return int(post_id.rsplit("_", 1)[1])


class PlannerAgent:
    name = "planner"
    subscribes = ("event:settled",)

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        # The verifier opened conflicts on this very settle. The steward corrects them before
        # the next settle is handled, so plan then instead of on facts that are about to change.
        if any(_number(c.id) > _number(post.id) for c in ctx.board.query(kind="conflict", status="open")):
            return [Post(kind="event", status="new", subject="settled", body={}, author=self.name)]
        pi = build_plan_input(ctx.board, ctx.tenant, ctx.ports.clock.now())
        fact_ids = tuple(
            p.id for p in ctx.board.query(kind="fact", status="active", limit=100_000)
            if p.subject.split("/", 1)[0] in _USED
        )
        forecast = replace(plan(pi), fact_ids=fact_ids)
        for old in ctx.board.query(kind="forecast", status="current"):
            ctx.board.set_status(old.id, "superseded", self.name, "new forecast")
        body = forecast.to_dict()
        if pi.consumed_since_snapshot or pi.not_rolled_forward or pi.stock_basis:
            body["assumptions"] = {"consumed_since_snapshot": pi.consumed_since_snapshot,
                                   "not_rolled_forward": list(pi.not_rolled_forward),
                                   "stock_basis": pi.stock_basis}
        return [Post(kind="forecast", status="current", subject="forecast",
                     body=body, author=self.name, depends_on=fact_ids)]


AGENTS = [PlannerAgent()]

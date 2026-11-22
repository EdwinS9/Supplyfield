"""The gate: rules on every outgoing action and records what people answer."""
from __future__ import annotations

from typing import Any

from gtl.contracts import (Action, Claim, Context, Decision, Evidence, GateVerdict, HumanAnswer,
                           Option, Post)
from gtl.govern.policy import Policy, assess, load_policy


def _option_from_body(body: dict[str, Any]) -> Option:
    return Option(title=body.get("title", ""),
                  actions=tuple(Action(**a) for a in body.get("actions", ())),
                  copies_saved=dict(body.get("copies_saved", {})),
                  cost_eur=float(body.get("cost_eur", 0.0)),
                  net_value_eur=float(body.get("net_value_eur", 0.0)),
                  notes=body.get("notes", ""))


class GateAgent:
    name = "gate"
    subscribes = ("action:requested", "event:human.answered")

    def __init__(self, policy: Policy | None = None, root: str = "tenants") -> None:
        self._policy = policy
        self._root = root

    def _get_policy(self, ctx: Context) -> Policy:
        if self._policy is None or self._policy.tenant_id != ctx.tenant["id"]:
            self._policy = load_policy(ctx.tenant["id"], self._root)
        return self._policy

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        if post.kind == "action":
            return self._rule(post, ctx)
        if post.kind == "event":
            return self._answer(post, ctx)
        return []

    # -- action:requested -------------------------------------------------
    def _rule(self, post: Post, ctx: Context) -> list[Post]:
        board: Any = ctx.board
        policy = self._get_policy(ctx)
        action = Action(**{k: v for k, v in post.body.items() if k in Action.__dataclass_fields__})
        verdict, approvers = assess(
            policy, action, agent_role_alias=ctx.tenant["autonomy"]["resolver_role"],
            board=board, tenant=ctx.tenant, now=ctx.ports.clock.now(),
            world=ctx.world, exclude_post_id=post.id)
        stored = {**post.body, "verdict": verdict.to_dict()}
        if verdict.outcome == "needs_approval":
            stored["approvers"] = [{k: a[k] for k in ("role", "person", "rule_id", "routed_from")}
                                   for a in approvers]
        board.update_body(post.id, stored, self.name)
        board.set_status(post.id, verdict.outcome, self.name, verdict.reason)
        if verdict.outcome != "needs_approval":
            return []
        options = self._options(action, board)
        return [self._decision(post, action, verdict, a, options, policy) for a in approvers]

    @staticmethod
    def _options(action: Action, board: Any) -> list[Option]:
        options = []
        for pid in action.context_post_ids:
            ref = board.get(pid)
            if ref is not None and ref.kind == "option":
                options.append(_option_from_body(ref.body))
        if not options:
            saved = action.params.get("copies_saved") or {}
            options.append(Option(title=action.reason or action.kind, actions=(action,),
                                  copies_saved=dict(saved), cost_eur=action.cost_eur,
                                  net_value_eur=float(action.params.get("net_value_eur", 0.0))))
        options.append(Option(title="Do not do this", actions=(), copies_saved={}, cost_eur=0.0,
                              net_value_eur=0.0))
        return options

    def _decision(self, post: Post, action: Action, verdict: GateVerdict, approver: dict[str, Any],
                  options: list[Option], policy: Policy) -> Post:
        problem = action.reason or verdict.reason
        if approver["routed_from"]:
            seat = policy.roles[approver["routed_from"]]
            why = f"is vacant until {seat.valid_from}" if seat.valid_from and not seat.vacant else "is vacant"
            problem += f" The {seat.title} seat {why}, so this goes to you under {approver['source_section']}."
        decision = Decision(problem=problem, options=tuple(options), recommended=0,
                            approver_role=approver["role"], approver_person=approver["person"],
                            deadline=action.params.get("deadline"), rule_id=approver["rule_id"])
        return Post(kind="decision", status="open", subject=f"{action.kind}: {approver['role']}",
                    body=decision.to_dict(), author=self.name, depends_on=(post.id,))

    # -- event:human.answered ---------------------------------------------
    def _answer(self, post: Post, ctx: Context) -> list[Post]:
        board: Any = ctx.board
        answer = HumanAnswer(**{k: v for k, v in post.body.items() if k in HumanAnswer.__dataclass_fields__})
        target = board.get(answer.post_id)
        summary = f"{answer.answered_by} answered {answer.post_id}"
        expected = None
        if target is not None and target.kind in ("decision", "defect"):
            expected = target.body.get("approver_role" if target.kind == "decision" else "owner_role")
        if target is not None and target.kind in ("decision", "defect") and answer.answered_by != expected:
            board.log_event(self.name, "gate.answer_rejected",
                            {"post_id": target.id, "answered_by": answer.answered_by, "expected": expected},
                            post.id)
            summary = f"{answer.answered_by} tried to answer {target.id} but it is assigned to {expected}"
        elif target is not None and target.kind == "decision":
            summary = self._answer_decision(target, answer, board)
        elif target is not None and target.kind == "defect":
            fixed = answer.approved is True
            board.set_status(target.id, "fixed" if fixed else "dismissed", self.name, answer.text)
            summary = f"{answer.answered_by} {'confirmed the fix of' if fixed else 'dismissed'} {target.id}"
        if answer.text:
            summary += f": {answer.text}"
        claim = Claim(entity="observation", key=f"human|{post.id}", field="text", value=summary,
                      as_of=answer.answered_at or ctx.ports.clock.now().isoformat(),
                      source_kind="human", evidence=(Evidence(source=f"human:{answer.answered_by}"),),
                      author=self.name)
        return [Post(kind="claim", status="proposed", subject=f"observation/human|{post.id}/text",
                     body=claim.to_dict(), author=self.name, depends_on=(post.id, answer.post_id))]

    def _answer_decision(self, target: Post, answer: HumanAnswer, board: Any) -> str:
        last = len(target.body.get("options", ())) - 1
        approved = answer.approved is True or (answer.choice is not None and answer.choice != last)
        if answer.choice is not None:
            chosen = answer.choice
        else:
            chosen = target.body.get("recommended", 0) if approved else last
        board.update_body(target.id, {**target.body, "chosen": chosen, "answered_by": answer.answered_by}, self.name)
        board.set_status(target.id, "answered", self.name)
        for ref in target.depends_on:
            act = board.get(ref)
            if act is None or act.kind != "action" or act.status != "needs_approval":
                continue
            siblings = [d for d in board.query(kind="decision") if ref in d.depends_on]
            if not approved:
                board.set_status(act.id, "rejected", self.name, answer.text)
                for d in siblings:
                    if d.id != target.id and d.status == "open":
                        board.set_status(d.id, "expired", self.name, "another approver rejected")
            elif all(d.status == "answered" and d.body.get("chosen") != len(d.body["options"]) - 1
                     for d in siblings):
                board.set_status(act.id, "approved", self.name, answer.text)
        return f"{answer.answered_by} {'approved' if approved else 'rejected'} {target.id}"

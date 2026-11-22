"""The four simulated ports (docs/gym.md section 1).

All four share one `WorldState` and one Board. Nothing here talks to the
network. The gate has already ruled on every action that reaches a port, so
the ports only carry the effect into the world and answer later, after the
supplier's or the person's response time.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from gtl.contracts import (Action, FileChange, GateVerdict, HumanAnswer, InboundMail, OutboundMail,
                           Post)
from gtl.gym.world import WorldState, apply_action, fact_sheet

COMMITMENTS = ("accept_offer", "place_order", "confirm_terms", "book_freight")

ReplyFn = Callable[[dict, dict, OutboundMail], str]
AnswerFn = Callable[[Any, Post], HumanAnswer]


def action_from_params(kind: str, params: dict[str, Any], key: str, requested_by: str = "resolver",
                       reason: str = "") -> Action:
    return Action(kind=kind, params=dict(params), requested_by=requested_by, reason=reason,  # type: ignore[arg-type]
                  idempotency_key=key, cost_eur=float(params.get("cost_eur") or 0.0))


class SimClock:
    def __init__(self, world: WorldState) -> None:
        self._world = world

    def now(self) -> datetime:
        return self._world.now


class SimFiles:
    """Fast mode: the runner injects claims directly, so there are no files."""

    def poll(self) -> list[FileChange]:
        return []

    def read(self, path: str, sha256: str | None = None) -> bytes:
        raise FileNotFoundError(path)

    def local_path(self, path: str, sha256: str | None = None) -> str:
        raise FileNotFoundError(path)


class SimMailbox:
    def __init__(self, world: WorldState, tenant: dict, replies: ReplyFn) -> None:
        self.world = world
        self.tenant = tenant
        self.replies = replies
        self.sent: list[OutboundMail] = []
        self.delivered: list[tuple[InboundMail, str]] = []  # (mail, supplier_id), read by the runner
        self.questions: list[tuple[str, datetime]] = []  # (supplier_id, when the reply is due)
        self._ids: dict[str, str] = {}
        self._threads: dict[str, str] = {}
        self._due: list[tuple[datetime, InboundMail, str]] = []

    def _supplier_for(self, address: str):  # type: ignore[no-untyped-def]
        want = address.strip().lower()
        return next((s for s in self.world.suppliers.values() if s.contact and s.contact.lower() == want), None)

    def send(self, mail: OutboundMail) -> str:
        if mail.idempotency_key and mail.idempotency_key in self._ids:
            return self._ids[mail.idempotency_key]
        world = self.world
        message_id = f"sim-msg-{len(self.sent) + 1}"
        self.sent.append(mail)
        if mail.idempotency_key:
            self._ids[mail.idempotency_key] = message_id
        # Like the provider: a reply stays in its thread, a fresh send opens one.
        self._threads[message_id] = mail.thread_id or message_id
        kind = mail.meta.get("action_kind")
        if kind in COMMITMENTS:
            # The remedy books its own emails; only the effect is carried in.
            action = action_from_params(kind, mail.meta.get("params") or {}, mail.idempotency_key,
                                        reason=mail.subject)
            apply_action(world, action, GateVerdict("allowed", None, "gate allowed it"))
            return message_id
        world.log("email", to=mail.to, message_id=message_id)
        sup = self._supplier_for(mail.to)
        if sup is not None:
            traits = {"language": sup.language, "forthcoming": sup.forthcoming, "honest": sup.honest,
                      "response_hours": sup.response_hours}
            body = self.replies(fact_sheet(world, sup.supplier_id), traits, mail)
            due = world.now + timedelta(hours=sup.response_hours)
            inbound = InboundMail(
                message_id=f"sim-in-{len(self.questions) + 1}", thread_id=message_id, inbox=mail.inbox,
                sender=sup.contact, subject=f"Re: {mail.subject}", body=body, received_at=due.isoformat())
            self._due.append((due, inbound, sup.supplier_id))
            self.questions.append((sup.supplier_id, due))
        return message_id

    def thread_of(self, message_id: str) -> str | None:
        return self._threads.get(message_id)

    def poll(self) -> list[InboundMail]:
        due = [d for d in self._due if d[0] <= self.world.now]
        self._due = [d for d in self._due if d[0] > self.world.now]
        for _, mail, supplier_id in due:
            self.delivered.append((mail, supplier_id))
        return [mail for _, mail, _ in due]


class SimControlRoom:
    def __init__(self, world: WorldState, board: Any, tenant: dict, answers: AnswerFn) -> None:
        self.world = world
        self.board = board
        self.tenant = tenant
        self.answers = answers
        self._applied: set[str] = set()
        self._scheduled: set[str] = set()
        self._due: list[tuple[datetime, HumanAnswer]] = []

    def publish(self, post: Post) -> None:
        if post.kind != "action":
            return
        body = post.body
        key = body.get("idempotency_key") or post.id
        if key in self._applied:
            return
        self._applied.add(key)
        params = body.get("params") or {}
        action = action_from_params(body["kind"], params, body.get("idempotency_key", ""),
                                    body.get("requested_by", "resolver"), body.get("reason", ""))
        ruled = body.get("verdict") or {}
        verdict = GateVerdict("allowed", ruled.get("rule_id"), ruled.get("reason", ""),
                              ruled.get("approver_role"), ruled.get("approver_person"))
        apply_action(self.world, action, verdict, tenant=self.tenant, human_done=post.status == "approved")

    def _person(self, post: Post):  # type: ignore[no-untyped-def]
        role = post.body.get("approver_role") or post.body.get("owner_role")
        return self.world.route_role(role)

    def _schedule(self, post: Post) -> None:
        self._scheduled.add(post.id)
        person = self._person(post)
        if person is None:
            return
        if post.kind == "decision":
            self.world.log("human_item", decision_id=post.id, approver=person.role_id)
        answer = self.answers(person, post)
        self._due.append((self.world.now + timedelta(hours=person.response_hours), answer))

    def poll(self) -> list[HumanAnswer]:
        items = self.board.query(kind="decision", status="open") + self.board.query(kind="defect", status="requested")
        for post in items:
            if post.id not in self._scheduled:
                self._schedule(post)
        now = self.world.now
        due = [d for d in self._due if d[0] <= now]
        self._due = [d for d in self._due if d[0] > now]
        out = []
        for when, answer in due:
            out.append(HumanAnswer(answer.post_id, answer.answered_by, answer.choice, answer.approved,
                                   answer.text, when.isoformat()))
        return out

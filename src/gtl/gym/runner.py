"""The episode runner (docs/gym.md section 8): the production agents play one scenario and get a euro score."""
from __future__ import annotations

import dataclasses
import importlib
from datetime import datetime
from typing import Any, Sequence

from gtl import dispatch, registry
from gtl.board import MemoryBoard
from gtl.contracts import Action, Claim, Context, GateVerdict, LLMResult, Ports, Post
from gtl.gym.adapters import SimClock, SimControlRoom, SimFiles, SimMailbox
from gtl.gym.scenario import Scenario
from gtl.gym.score import Result, baselines, offer_outcomes, score
from gtl.gym.scripted import LIAISON, ScriptedLiaison, reply_claims, scripted_answer, scripted_reply
from gtl.gym.world import WorldState, apply_action, refresh_claims, step_hour
from gtl.govern.policy import seed_config_claims
from gtl.testing import FakeLLM

MAIL_READER = "mail_reader"


class BudgetExceeded(RuntimeError):
    """Raised for every call past the episode limit; agents then use their rule fallback."""


class BudgetedLLM:
    """Counts calls per role and refuses calls past the limit of that role."""

    def __init__(self, inner: Any, limits: dict[str, int]) -> None:
        self.inner = inner
        self.limits = limits
        self.calls: dict[str, int] = {}

    def complete(self, role: str, system: str, user: str, **kw: Any) -> LLMResult:
        limit = self.limits.get(role)
        if limit is not None and self.calls.get(role, 0) >= limit:
            raise BudgetExceeded(f"{role}: limit of {limit} calls reached")
        self.calls[role] = self.calls.get(role, 0) + 1
        return self.inner.complete(role, system, user, **kw)


def _no_agent_llm() -> FakeLLM:
    def respond(role: str, *_: Any) -> str:
        if role == "agent":
            raise RuntimeError("no LLM: agents use their rule fallback")
        return ""
    return FakeLLM(respond)


def _persona_fns(personas: str, llm: Any) -> tuple[Any, Any]:
    """Reply and answer functions: scripted, or the LLM personas when they exist (each falls back to scripted)."""
    if personas == "llm":
        try:
            mod = importlib.import_module("gtl.gym.personas")
        except ImportError:
            mod = None
        reply_fn, answer_fn = getattr(mod, "reply_to_mail", None), getattr(mod, "answer_item", None)

        def reply(sheet: dict, traits: dict, mail: Any) -> str:
            if reply_fn is not None:
                try:
                    return reply_fn(sheet, traits, mail, llm)
                except Exception:  # noqa: BLE001 - a failed persona call means the scripted reply
                    pass
            return scripted_reply(sheet, traits, mail)

        def answer(person: Any, post: Post) -> Any:
            if answer_fn is not None:
                try:
                    return answer_fn(person, post, llm)
                except Exception:  # noqa: BLE001
                    pass
            return scripted_answer(person, post)
        return reply, answer
    return scripted_reply, scripted_answer


# The world labels every claim "sim". The Board needs the kind a reader would give it, because the
# verifier ranks sources: a document beats the ERP, the ERP beats the tracker.
_SOURCE_KINDS = {"book": "erp", "erp": "erp", "tracker": "tracker", "offer": "mail"}


def _claim_post(claim: Claim) -> Post:
    name = claim.evidence[0].source.split(":")[1] if claim.evidence and ":" in claim.evidence[0].source else ""
    claim = dataclasses.replace(claim, source_kind=_SOURCE_KINDS.get(name, "document"))  # type: ignore[arg-type]
    return Post(kind="claim", status="proposed", subject=f"{claim.entity}/{claim.key}/{claim.field}",
                body=claim.to_dict(), author=claim.author)


class _Observables:
    """Claims the world makes visible at set times; `on_request` ones wait until a supplier is asked."""

    def __init__(self, world: WorldState) -> None:
        self.queue: list[tuple[datetime, Claim]] = []
        self.held: list[Claim] = []
        for when, claim in world.truth["observables"]:
            if "on_request" in claim.flags:
                self.held.append(claim)
            else:
                self.queue.append((when, claim))

    def asked(self, supplier_id: str, when: datetime) -> None:
        mine = [c for c in self.held if c.evidence[0].source.endswith(f":{supplier_id}")]
        for c in mine:
            self.held.remove(c)
            self.queue.append((when, dataclasses.replace(c, as_of=when.isoformat())))

    def due(self, now: datetime) -> list[Claim]:
        out = [c for t, c in self.queue if t <= now]
        self.queue = [(t, c) for t, c in self.queue if t > now]
        return out


def _count_violations(board: Any, world: WorldState, counted: set[str]) -> None:
    for post in board.query(kind="action", status="blocked", limit=100_000):
        if post.id in counted:
            continue
        counted.add(post.id)
        b = post.body
        action = Action(**{k: v for k, v in b.items() if k in Action.__dataclass_fields__})
        v = b.get("verdict") or {}
        apply_action(world, action, GateVerdict("blocked", v.get("rule_id"), v.get("reason", "")))


def _mark_answered(board: Any, message_id: str) -> None:
    """What the mail reader does for a reply: the question that mail asked is answered."""
    for action in board.query(kind="action", status="executed", limit=100_000):
        if action.body.get("message_id") != message_id:
            continue
        params = action.body.get("params", {})
        for qid in params.get("question_ids") or [params.get("question_id")]:
            question = board.get(str(qid or ""))
            if question is not None and question.status == "asked":
                board.set_status(question.id, "answered", MAIL_READER, "supplier replied")


def _trace(board: Any) -> list[dict]:
    out: list[dict] = []
    for p in board.query(kind="action", limit=100_000):
        b = p.body
        out.append({"type": "action", "post_id": p.id, "kind": b.get("kind"), "status": p.status,
                    "cost_eur": b.get("cost_eur", 0.0), "reason": b.get("reason", ""),
                    "at": p.created_at, "by": b.get("requested_by")})
    for p in board.query(kind="option", limit=100_000):
        if p.body.get("context"):  # offers: what the resolver knew about each one it took or passed over
            out.append({"type": "option", "post_id": p.id, "subject": p.subject, "status": p.status,
                        "net_value_eur": p.body.get("net_value_eur"), "cost_eur": p.body.get("cost_eur"),
                        "context": p.body["context"], "by": p.author})
    for p in board.query(kind="decision", limit=100_000):
        b = p.body
        chosen = b.get("chosen")
        out.append({"type": "decision", "post_id": p.id, "problem": b.get("problem"), "status": p.status,
                    "approver_role": b.get("approver_role"), "at": p.created_at,
                    "recommended": b.get("recommended"), "chosen": chosen, "answered_by": b.get("answered_by"),
                    "answer": b["options"][chosen]["title"] if chosen is not None else None})
    return out


def run_episode(scenario: Scenario, tenant: dict, *, trace: bool = False, **kwargs: Any) -> Result:
    """Play one scenario and score it. Options are those of `play_episode`."""
    return play_episode(scenario, tenant, trace=trace, **kwargs)[0]


def play_episode(scenario: Scenario, tenant: dict, *, playbooks: dict[str, str] | None = None, llm: Any = None,
                 agents: Sequence[Any] | None = None, personas: str = "scripted", max_agent_calls: int = 25,
                 max_persona_calls: int = 12, trace: bool = False) -> tuple[Result, MemoryBoard, WorldState]:
    """Like `run_episode`, but also returns the Board and the final world, for diagnosis."""
    world = scenario.build(tenant)
    clock = SimClock(world)
    board = MemoryBoard(now=clock.now)
    budget = BudgetedLLM(llm if llm is not None else _no_agent_llm(),
                         {"agent": max_agent_calls, "persona": max_persona_calls})
    reply_fn, answer_fn = _persona_fns(personas, budget)
    mailbox = SimMailbox(world, tenant, reply_fn)
    ports = Ports(files=SimFiles(), mail=mailbox, control_room=SimControlRoom(world, board, tenant, answer_fn),
                  clock=clock)
    ctx = Context(board=board, ports=ports, llm=budget, tenant=tenant, world="sim", playbooks=playbooks or {})
    agents = list(agents) if agents is not None else registry.load_agents()
    if personas == "scripted":
        # Scripted counterparts answer with known facts, which the runner turns into claims itself.
        # The mail reader is only needed when a persona writes free text, so it sits this mode out.
        agents = [a for a in agents if a.name != MAIL_READER]
    if not any(a.name == LIAISON for a in agents):
        agents.append(ScriptedLiaison())  # the stand-in, only when no liaison is registered
    has_reader = any(a.name == MAIL_READER for a in agents)

    seed_config_claims(ctx)
    sent: dict[tuple[str, str, str], Any] = {}
    observables = _Observables(world)
    counted: set[str] = set()
    seen_questions = 0

    while world.now < world.deadline:
        for claim in refresh_claims(world, sent):
            board.append(_claim_post(claim))
        for claim in observables.due(world.now):
            board.append(_claim_post(claim))
        if not has_reader:
            # No mail reader yet: a reply is read before the agents look, so a reminder never crosses it.
            mailbox.poll()
            delivered, mailbox.delivered = mailbox.delivered, []
            for mail, supplier_id in delivered:
                for claim in reply_claims(world, supplier_id, mail, world.now.isoformat()):
                    board.append(_claim_post(claim))
                _mark_answered(board, mail.thread_id)
        dispatch.cycle(ctx, agents)
        mailbox.delivered.clear()
        for supplier_id, due in mailbox.questions[seen_questions:]:
            observables.asked(supplier_id, due)
        seen_questions = len(mailbox.questions)
        _count_violations(board, world, counted)
        step_hour(world)

    result = score(world, tenant)
    idle, best, remedies = baselines(scenario, tenant)
    result.with_baselines(idle, best)
    result.breakdown["oracle_remedies"] = remedies
    for status in ("scheduled", "rejected"):
        result.breakdown[f"{status}_remedies"] = sorted(
            {e["remedy_id"] for e in world.ledger if e["kind"] == "action" and e.get("status") == status})
    result.breakdown["bad_unit_batches"] = sorted({e["batch"] for e in world.ledger if e["kind"] == "bad_units"})
    result.breakdown["packed"] = {p: world.packed.get(p, 0.0) - world.packed_start.get(p, 0.0) for p in world.packed}
    result.breakdown["outcomes"] = offer_outcomes(world)
    result.breakdown["agent_errors"] = sum(1 for e in board.events_since(0) if e["type"] == "agent.error")
    result.llm_calls = dict(budget.calls)
    if trace:
        result.trace = _trace(board)
    return result, board, world

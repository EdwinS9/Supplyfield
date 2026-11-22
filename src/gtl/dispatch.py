"""The dispatcher: turns port input into events and runs agents over the Board."""
from __future__ import annotations

import time
import traceback
from typing import Any, Callable, Sequence

from gtl.contracts import Context, Post, topic

try:
    from gtl.plan import PLANNED_ACTIONS
except ImportError:
    PLANNED_ACTIONS = ()


# The truth pipeline settles before anything goes out: these agents, plus the planner and the resolver
# that turn settled facts into a forecast, take their work before liaisons, gate and executor.
# Within the truth pipeline, tables are read and verified before any document or mail is read: the
# document reader takes the ids it may use, and what it may skip, from verified facts.
TRUTH_STAGES = (("table_reader", "verifier", "propagator"),
                ("document_reader", "mail_reader", "data_steward"))
TRUTH_AGENTS = tuple(name for stage in TRUTH_STAGES for name in stage)
FORECAST_AGENTS = ("planner", "resolver")


def _event(subject: str, body: dict[str, Any], author: str, depends_on: tuple[str, ...] = ()) -> Post:
    return Post(kind="event", status="new", subject=subject, body=body, author=author, depends_on=depends_on)


def _hour_key(iso: str) -> str:
    return iso[:13]  # YYYY-MM-DDTHH


def _poll(board: Any, name: str, fn: Callable[[], Any]) -> list[Any]:
    """Poll one port. A port that fails is logged and skipped; it never stops the run."""
    try:
        return list(fn())
    except Exception:  # noqa: BLE001 - the outside world may be down
        board.log_event("dispatcher", "port.error", {"port": name, "traceback": traceback.format_exc()})
        return []


def poll_ports(ctx: Context) -> int:
    board: Any = ctx.board
    n = 0
    for ch in _poll(board, "files", ctx.ports.files.poll):
        ref = f"file:{ch.path}@{ch.sha256}"
        board.append(_event("file.changed", ch.to_dict(), "watcher", (ref,)))
        n += 1
    for mail in _poll(board, "mail", ctx.ports.mail.poll):
        board.append(_event("mail.received", mail.to_dict(), "mailbox"))
        n += 1
    for ans in _poll(board, "control_room", ctx.ports.control_room.poll):
        board.append(_event("human.answered", ans.to_dict(), ans.answered_by))
        n += 1
    now = ctx.ports.clock.now().isoformat()
    last = board.latest_event_post("tick")
    if last is None or _hour_key(last.body.get("now", "")) != _hour_key(now):
        board.append(_event("tick", {"now": now}, "clock"))
        n += 1
    return n


def cycle(ctx: Context, agents: Sequence[Any], max_steps: int = 5000) -> int:
    board: Any = ctx.board
    poll_ports(ctx)
    handled = 0
    first = TRUTH_AGENTS + FORECAST_AGENTS
    stages = [[a for a in agents if a.name in names] for names in (*TRUTH_STAGES, FORECAST_AGENTS)]
    rest = [a for a in agents if a.name not in first]
    while handled < max_steps:
        # The earliest stage with work goes first, so a later stage always sees what the earlier ones settled.
        work = next((w for group in stages if group and (w := board.next_unhandled(group)) is not None), None)
        if work is None:
            # Truth work is drained: settle now, so the forecast does not wait for questions and mails.
            if board.facts_changed_since_settle(PLANNED_ACTIONS):
                board.append(_event("settled", {}, "dispatcher"))
                continue
            work = board.next_unhandled(rest) if rest else None
            if work is None:
                break
        agent, post = work
        tp = topic(post)
        try:
            for new in agent.handle(post, ctx):
                if new.kind == "action" and new.status != "requested":
                    # Every action must pass the gate: nobody may post one that is already ruled on.
                    board.log_event(agent.name, "agent.violation",
                                    {"agent": agent.name, "post_id": post.id, "topic": tp,
                                     "reason": f"action posted with status {new.status!r}; dropped"}, post.id)
                    continue
                board.append(new)
        except Exception:  # noqa: BLE001 - one bad agent must not stop the cycle
            board.log_event(agent.name, "agent.error",
                            {"agent": agent.name, "post_id": post.id, "topic": tp,
                             "traceback": traceback.format_exc()}, post.id)
        board.mark_handled(agent.name, post.id, tp)
        handled += 1
    return handled


def run(ctx: Context, agents: Sequence[Any], poll_seconds: float,
        stop: Callable[[], bool] = lambda: False) -> None:
    while not stop():
        cycle(ctx, agents)
        if stop():
            break
        time.sleep(poll_seconds)

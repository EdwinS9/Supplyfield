from gtl import dispatch
from gtl.contracts import FileChange, Post
from gtl.testing import make_context


class Emit:
    name = "emitter"
    subscribes = ("claim:proposed",)

    def handle(self, post, ctx):
        return [Post(kind="fact", status="active", subject="f", author=self.name,
                     body={"entity": "e", "key": "k", "field": "f", "value": 1, "as_of": "t",
                           "claim_ids": [post.id], "checks_passed": []})]


class Boom:
    name = "boom"
    subscribes = ("claim:proposed",)

    def handle(self, post, ctx):
        raise RuntimeError("kaput")


class Verified:
    name = "ver"
    subscribes = ("claim:verified",)

    def __init__(self):
        self.seen = []

    def handle(self, post, ctx):
        self.seen.append(post.id)
        return []


def claim():
    return Post(kind="claim", status="proposed", subject="c", body={}, author="r")


def test_agent_emits_fact_and_settled_once():
    ctx = make_context()
    ctx.board.append(claim())
    n = dispatch.cycle(ctx, [Emit()])
    assert n == 1
    assert len(ctx.board.query(kind="fact")) == 1
    settled = [p for p in ctx.board.query(kind="event") if p.subject == "settled"]
    assert len(settled) == 1
    assert dispatch.cycle(ctx, [Emit()]) == 0
    assert len([p for p in ctx.board.query(kind="event") if p.subject == "settled"]) == 1
    # a status change on a fact makes another settled due
    ctx.board.set_status(ctx.board.query(kind="fact")[0].id, "superseded", "x")
    dispatch.cycle(ctx, [Emit()])
    assert len([p for p in ctx.board.query(kind="event") if p.subject == "settled"]) == 2


def test_forecast_resets_settled_baseline():
    ctx = make_context()
    ctx.board.append(claim())
    dispatch.cycle(ctx, [Emit()])
    ctx.board.append(Post(kind="forecast", status="current", subject="f", body={}, author="p"))
    assert not ctx.board.facts_changed_since_settle()


def test_executed_plan_action_makes_another_settled_due():
    ctx = make_context()
    ctx.board.append(Post(kind="forecast", status="current", subject="f", body={}, author="p"))
    for kind in ("send_email", "request_pickup"):
        act = ctx.board.append(Post(kind="action", status="allowed", subject=kind, body={"kind": kind},
                                    author="gate"))
        ctx.board.set_status(act.id, "executed", "executor")
        assert ctx.board.facts_changed_since_settle(("request_pickup",)) == (kind == "request_pickup")
    assert not ctx.board.facts_changed_since_settle()  # kinds are the caller's knowledge


def test_status_change_redelivers():
    ctx = make_context()
    v = Verified()
    p = ctx.board.append(claim())
    dispatch.cycle(ctx, [v])
    assert v.seen == []
    ctx.board.set_status(p.id, "verified", "x")
    dispatch.cycle(ctx, [v])
    assert v.seen == [p.id]
    dispatch.cycle(ctx, [v])
    assert v.seen == [p.id]


def test_agent_error_does_not_stop_cycle():
    ctx = make_context()
    ctx.board.append(claim())
    dispatch.cycle(ctx, [Boom(), Emit()])
    errs = [e for e in ctx.board.events_since(0) if e["type"] == "agent.error"]
    assert len(errs) == 1 and "kaput" in errs[0]["payload"]["traceback"]
    assert errs[0]["payload"]["agent"] == "boom"
    assert len(ctx.board.query(kind="fact")) == 1
    assert dispatch.cycle(ctx, [Boom(), Emit()]) == 0


def test_tick_on_hour_change():
    ctx = make_context()
    clock = ctx.ports.clock
    dispatch.poll_ports(ctx)
    dispatch.poll_ports(ctx)
    ticks = lambda: [p for p in ctx.board.query(kind="event") if p.subject == "tick"]
    assert len(ticks()) == 1
    clock.advance(minutes=30)
    dispatch.poll_ports(ctx)
    assert len(ticks()) == 1
    clock.advance(hours=1)
    dispatch.poll_ports(ctx)
    assert len(ticks()) == 2
    assert ticks()[1].body["now"] == clock.now().isoformat()


def test_ports_become_events():
    from gtl.contracts import HumanAnswer, InboundMail
    ctx = make_context()
    ctx.ports.files.push(FileChange(path="a/b.csv", kind="added", sha256="abc", previous_sha256=None))
    ctx.ports.mail.deliver(InboundMail("m1", "t1", "supply", "x@y", "s", "b", "now"))
    ctx.ports.control_room.answer(HumanAnswer(post_id="decision_1", answered_by="buyer", choice=0))
    dispatch.poll_ports(ctx)
    ev = {p.subject: p for p in ctx.board.query(kind="event")}
    assert ev["file.changed"].depends_on == ("file:a/b.csv@abc",)
    assert ev["file.changed"].body["path"] == "a/b.csv"
    assert ev["mail.received"].body["message_id"] == "m1"
    assert ev["human.answered"].body["choice"] == 0


def test_run_stops():
    ctx = make_context()
    calls = []
    dispatch.run(ctx, [], 0, stop=lambda: bool(calls.append(1)) or len(calls) > 2)


class _Sneaky:
    """An agent that tries to post an action that skips the gate."""
    name = "sneaky"
    subscribes = ("event:tick",)

    def handle(self, post, ctx):
        from gtl.contracts import Post
        return [Post(kind="action", status="allowed", subject="x", author=self.name,
                     body={"kind": "send_email", "params": {}, "requested_by": self.name,
                           "reason": "", "idempotency_key": "k"})]


def test_action_that_skips_the_gate_is_dropped():
    from gtl import dispatch
    from gtl.testing import make_context
    ctx = make_context()
    dispatch.cycle(ctx, [_Sneaky()])
    assert ctx.board.query(kind="action") == []
    assert any(e["type"] == "agent.violation" for e in ctx.board.events_since(0))


def test_failing_port_is_logged_and_does_not_stop_the_cycle():
    from gtl import dispatch
    from gtl.testing import make_context
    ctx = make_context()

    def boom():
        raise OSError("portal down")

    ctx.ports.files.poll = boom
    dispatch.cycle(ctx, [])
    assert any(e["type"] == "port.error" for e in ctx.board.events_since(0))


class _Planner:
    name = "planner"
    subscribes = ("event:settled",)

    def __init__(self, order):
        self.order = order

    def handle(self, post, ctx):
        self.order.append("forecast")
        return [Post(kind="forecast", status="current", subject="f", body={}, author="planner")]


class _Liaison:
    name = "supply_liaison"
    subscribes = ("question:open",)

    def __init__(self, order):
        self.order = order

    def handle(self, post, ctx):
        self.order.append("post")
        return []


def test_forecast_comes_before_liaison_work():
    order = []
    agents = [_Planner(order), _Liaison(order)]
    ctx = make_context()
    ctx.board.append(Post(kind="fact", status="active", subject="f", author="x",
                          body={"entity": "e", "key": "k", "field": "f", "value": 1, "as_of": "t",
                                "claim_ids": [], "checks_passed": []}))
    for i in range(50):
        ctx.board.append(Post(kind="question", status="open", subject=f"q{i}", body={}, author="steward"))
    dispatch.cycle(ctx, agents)
    assert order[0] == "forecast" and order.count("post") == 50
    assert len([p for p in ctx.board.query(kind="event") if p.subject == "settled"]) == 1
    dispatch.cycle(ctx, agents)
    assert len([p for p in ctx.board.query(kind="event") if p.subject == "settled"]) == 1


class _Reader:
    """A reader stand-in: one claim per file event of its kind; it records how many facts it saw."""
    subscribes = ("event:file.changed",)

    def __init__(self, name, suffix, seen):
        self.name, self.suffix, self.seen = name, suffix, seen

    def handle(self, post, ctx):
        if not post.body["path"].endswith(self.suffix):
            return []
        self.seen.append((self.name, len(ctx.board.query(kind="fact", status="active"))))
        return [Post(kind="claim", status="proposed", subject=post.body["path"], body={}, author=self.name)]


class _Verifier:
    name = "verifier"
    subscribes = ("claim:proposed",)

    def handle(self, post, ctx):
        ctx.board.set_status(post.id, "verified", self.name)
        return [Post(kind="fact", status="active", subject=post.subject, author=self.name,
                     body={"entity": "e", "key": post.subject, "field": "f", "value": 1, "as_of": "t",
                           "claim_ids": [post.id], "checks_passed": []})]


def test_tables_are_read_and_verified_before_any_document_is_read():
    seen = []
    agents = [_Reader("document_reader", ".pdf", seen), _Reader("table_reader", ".csv", seen), _Verifier()]
    ctx = make_context()
    # The documents arrive first; the tables they depend on arrive after them.
    for path in ("a.pdf", "b.pdf", "orders.csv", "parts.csv"):
        ctx.board.append(Post(kind="event", status="new", subject="file.changed", author="watcher",
                              body={"path": path, "kind": "added", "sha256": "0" * 64}))
    dispatch.cycle(ctx, agents)
    assert [name for name, _ in seen] == ["table_reader", "table_reader", "document_reader", "document_reader"]
    # Both tables were verified before the first document was opened, and the first document's
    # own claim was verified before the second document was opened.
    assert [facts for name, facts in seen if name == "document_reader"] == [2, 3]

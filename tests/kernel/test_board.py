import pytest

from gtl.contracts import HumanAnswer, Post
from gtl.testing import FakeClock, MemoryBoard


def mk(kind="claim", status="proposed", subject="s", body=None, **kw):
    return Post(kind=kind, status=status, subject=subject, body=body or {}, author="t", **kw)


def test_ids_use_global_counter_and_timestamps():
    clock = FakeClock()
    b = MemoryBoard(now=clock.now)
    a = b.append(mk())
    c = b.append(mk(kind="fact", status="active"))
    assert a.id == "claim_1" and c.id == "fact_2"
    assert a.created_at == clock.now().isoformat() == a.updated_at


def test_invalid_status_rejected():
    b = MemoryBoard()
    with pytest.raises(ValueError):
        b.append(mk(status="bogus"))
    p = b.append(mk())
    with pytest.raises(ValueError):
        b.set_status(p.id, "active", "x")


def test_events_written():
    b = MemoryBoard()
    p = b.append(mk())
    b.set_status(p.id, "verified", "v", note="ok")
    ev = b.events_since(0)
    assert [e["type"] for e in ev] == ["post.append", "post.status"]
    assert ev[1]["payload"]["to"] == "verified"
    assert b.events_since(ev[0]["id"]) == [ev[1]]
    assert b.last_event_id() == ev[1]["id"]
    b.update_body(p.id, {"x": 1}, "a")
    assert b.get(p.id).body == {"x": 1}
    assert b.events_since(0)[-1]["type"] == "post.body"


def test_query_filters():
    b = MemoryBoard()
    p1 = b.append(mk(subject="stock/a/qty"))
    b.append(mk(subject="stock/b/qty"))
    b.append(mk(kind="fact", status="active", subject="po/1"))
    mark = b.last_event_id()
    b.append(mk(subject="stock/c/qty"))
    assert len(b.query(kind="claim")) == 3
    assert [p.subject for p in b.query(subject_prefix="stock/a")] == ["stock/a/qty"]
    assert len(b.query(kind="fact", status="active")) == 1
    assert len(b.query(status="verified")) == 0
    assert [p.subject for p in b.query(since_event=mark)] == ["stock/c/qty"]
    assert len(b.query(limit=2)) == 2
    assert b.query()[0].id == p1.id


def test_facts_skip_quarantined():
    b = MemoryBoard()
    base = {"entity": "stock", "key": "k1", "field": "qty", "value": 5, "as_of": "t",
            "claim_ids": ["claim_1"], "checks_passed": ["c"]}
    b.append(mk(kind="fact", status="active", body=base))
    b.append(mk(kind="fact", status="active", body={**base, "key": "k2", "quarantined": True}))
    b.append(mk(kind="fact", status="superseded", body={**base, "key": "k3"}))
    facts = b.facts()
    assert [f.key for f in facts] == ["k1"]
    assert facts[0].claim_ids == ("claim_1",)
    assert b.facts(entity="po") == []
    assert len(b.facts(key_prefix="k")) == 1


def test_dependents():
    b = MemoryBoard()
    p = b.append(mk(depends_on=("file:a.csv@abc",)))
    q = b.append(mk(depends_on=(p.id, "file:a.csv@abc")))
    assert b.dependents("file:a.csv@abc") == [p.id, q.id]
    assert b.dependents(p.id) == [q.id]
    assert b.get(q.id).depends_on == (p.id, "file:a.csv@abc")


class Ag:
    name = "a"
    subscribes = ("claim:proposed", "event:tick")


def test_handled_bookkeeping():
    b = MemoryBoard()
    p = b.append(mk())
    agent, post = b.next_unhandled([Ag()])
    assert post.id == p.id
    b.mark_handled("a", p.id, "claim:proposed")
    assert b.next_unhandled([Ag()]) is None
    b.set_status(p.id, "verified", "x")
    assert b.next_unhandled([Ag()]) is None  # not subscribed to the new topic


def test_answers():
    b = MemoryBoard()
    b.add_answer(HumanAnswer(post_id="decision_1", answered_by="buyer", choice=1))
    got = b.take_answers()
    assert got[0].choice == 1 and got[0].post_id == "decision_1"
    assert b.take_answers() == []


def test_file_database_wal(tmp_path):
    from gtl.board import SqliteBoard
    b = SqliteBoard(str(tmp_path / "b.db"))
    b.append(mk())
    assert SqliteBoard(str(tmp_path / "b.db")).get("claim_1") is not None

"""The runtime Board: posts, events, dependencies and bookkeeping in SQLite."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import fields
from datetime import datetime
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from gtl.contracts import STATUSES, Fact, HumanAnswer, Post

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, type TEXT, post_id TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY, kind TEXT, status TEXT, subject TEXT, body TEXT,
    author TEXT, created_at TEXT, updated_at TEXT, supersedes TEXT);
CREATE TABLE IF NOT EXISTS deps(post_id TEXT, ref TEXT);
CREATE INDEX IF NOT EXISTS deps_ref ON deps(ref);
CREATE INDEX IF NOT EXISTS deps_post ON deps(post_id);
CREATE INDEX IF NOT EXISTS posts_kind_status ON posts(kind, status);
CREATE TABLE IF NOT EXISTS handled(agent TEXT, post_id TEXT, topic TEXT, PRIMARY KEY(agent, post_id, topic));
CREATE TABLE IF NOT EXISTS answers(id INTEGER PRIMARY KEY, post_id TEXT, payload TEXT, consumed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS counter(n INTEGER);
"""

_FACT_FIELDS = {f.name for f in fields(Fact)}


def _berlin_now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Berlin"))


class SqliteBoard:
    def __init__(self, path: str = ":memory:", now: Callable[[], datetime] | None = None) -> None:
        self._now = now or _berlin_now
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        if self._db.execute("SELECT COUNT(*) FROM counter").fetchone()[0] == 0:
            self._db.execute("INSERT INTO counter(n) VALUES (0)")

    # -- helpers -----------------------------------------------------------
    def _ts(self) -> str:
        return self._now().isoformat()

    def _row_to_post(self, row: sqlite3.Row) -> Post:
        deps = tuple(r[0] for r in self._db.execute(
            "SELECT ref FROM deps WHERE post_id=? ORDER BY rowid", (row["id"],)))
        return Post(kind=row["kind"], status=row["status"], subject=row["subject"],
                    body=json.loads(row["body"]), author=row["author"], id=row["id"],
                    created_at=row["created_at"], updated_at=row["updated_at"],
                    depends_on=deps, supersedes=row["supersedes"])

    def _event(self, actor: str, type_: str, payload: dict[str, Any], post_id: str | None) -> int:
        cur = self._db.execute(
            "INSERT INTO events(ts, actor, type, post_id, payload) VALUES (?,?,?,?,?)",
            (self._ts(), actor, type_, post_id, json.dumps(payload, default=str)))
        return int(cur.lastrowid or 0)

    @staticmethod
    def _check_status(kind: str, status: str) -> None:
        allowed = STATUSES.get(kind)
        if allowed is None:
            raise ValueError(f"unknown post kind {kind!r}")
        if status not in allowed:
            raise ValueError(f"status {status!r} is not allowed for kind {kind!r}; allowed: {allowed}")

    # -- Board protocol ----------------------------------------------------
    def append(self, post: Post) -> Post:
        self._check_status(post.kind, post.status)
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute("UPDATE counter SET n = n + 1")
                n = self._db.execute("SELECT n FROM counter").fetchone()[0]
                pid = f"{post.kind}_{n}"
                ts = self._ts()
                self._db.execute(
                    "INSERT INTO posts(id, kind, status, subject, body, author, created_at, updated_at, supersedes)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (pid, post.kind, post.status, post.subject, json.dumps(post.body, default=str),
                     post.author, ts, ts, post.supersedes))
                for ref in post.depends_on:
                    self._db.execute("INSERT INTO deps(post_id, ref) VALUES (?,?)", (pid, ref))
                self._event(post.author, "post.append",
                            {"kind": post.kind, "status": post.status, "subject": post.subject}, pid)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            stored = self.get(pid)
        assert stored is not None
        return stored

    def get(self, post_id: str) -> Post | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
            return self._row_to_post(row) if row else None

    def query(self, kind: str | None = None, status: str | None = None,
              subject_prefix: str | None = None, since_event: int = 0, limit: int = 1000) -> list[Post]:
        sql = "SELECT * FROM posts WHERE 1=1"
        args: list[Any] = []
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        if status is not None:
            sql += " AND status=?"
            args.append(status)
        if subject_prefix is not None:
            sql += " AND substr(subject, 1, ?)=?"
            args += [len(subject_prefix), subject_prefix]
        if since_event:
            sql += " AND id IN (SELECT post_id FROM events WHERE id>? AND post_id IS NOT NULL)"
            args.append(since_event)
        sql += " ORDER BY rowid LIMIT ?"
        args.append(limit)
        with self._lock:
            return [self._row_to_post(r) for r in self._db.execute(sql, args).fetchall()]

    def set_status(self, post_id: str, status: str, actor: str, note: str = "") -> Post:
        with self._lock:
            post = self.get(post_id)
            if post is None:
                raise KeyError(post_id)
            self._check_status(post.kind, status)
            self._db.execute("UPDATE posts SET status=?, updated_at=? WHERE id=?",
                             (status, self._ts(), post_id))
            self._event(actor, "post.status",
                        {"from": post.status, "to": status, "note": note, "kind": post.kind}, post_id)
            out = self.get(post_id)
        assert out is not None
        return out

    def facts(self, entity: str | None = None, key_prefix: str | None = None):  # -> list[Fact]
        out: list[Fact] = []
        with self._lock:  # bodies only: no dependency lookups and no row limit, this runs on every planning step
            rows = self._db.execute("SELECT body FROM posts WHERE kind='fact' AND status='active' ORDER BY rowid"
                                    ).fetchall()
        for row in rows:
            b = json.loads(row["body"])
            if b.get("quarantined"):
                continue
            if entity is not None and b.get("entity") != entity:
                continue
            if key_prefix is not None and not str(b.get("key", "")).startswith(key_prefix):
                continue
            data = {k: v for k, v in b.items() if k in _FACT_FIELDS}
            data["claim_ids"] = tuple(data.get("claim_ids", ()))
            data["checks_passed"] = tuple(data.get("checks_passed", ()))
            out.append(Fact(**data))
        return out

    def events_since(self, event_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM events WHERE id>? ORDER BY id", (event_id,)).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "actor": r["actor"], "type": r["type"],
                 "post_id": r["post_id"], "payload": json.loads(r["payload"])} for r in rows]

    def dependents(self, ref: str) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT post_id FROM deps WHERE ref=? GROUP BY post_id ORDER BY MIN(rowid)", (ref,)).fetchall()
        return [r[0] for r in rows]

    # -- extras for the dispatcher and the API ------------------------------
    def next_unhandled(self, agents: Sequence[Any]) -> tuple[Any, Post] | None:
        best: tuple[int, Any, str] | None = None
        with self._lock:
            for agent in agents:
                for tp in agent.subscribes:
                    head, _, tail = tp.partition(":")
                    if head == "event":
                        cond, args = "p.kind='event' AND p.subject=?", [tail]
                    else:
                        cond, args = "p.kind=? AND p.status=?", [head, tail]
                    row = self._db.execute(
                        f"SELECT p.rowid AS rid, p.id AS id FROM posts p WHERE {cond} AND NOT EXISTS ("
                        "SELECT 1 FROM handled h WHERE h.agent=? AND h.post_id=p.id AND h.topic=?) "
                        "ORDER BY p.rowid LIMIT 1", [*args, agent.name, tp]).fetchone()
                    if row and (best is None or row["rid"] < best[0]):
                        best = (row["rid"], agent, row["id"])
            if best is None:
                return None
            post = self.get(best[2])
        assert post is not None
        return best[1], post

    def mark_handled(self, agent_name: str, post_id: str, topic_: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO handled(agent, post_id, topic) VALUES (?,?,?)",
                             (agent_name, post_id, topic_))

    def add_answer(self, answer: HumanAnswer) -> None:
        with self._lock:
            self._db.execute("INSERT INTO answers(post_id, payload) VALUES (?,?)",
                             (answer.post_id, json.dumps(answer.to_dict())))
            self._event(answer.answered_by, "answer.added", answer.to_dict(), answer.post_id)

    def take_answers(self) -> list[HumanAnswer]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM answers WHERE consumed=0 ORDER BY id").fetchall()
            out = []
            for r in rows:
                self._db.execute("UPDATE answers SET consumed=1 WHERE id=?", (r["id"],))
                out.append(HumanAnswer(**json.loads(r["payload"])))
            return out

    def update_body(self, post_id: str, body: dict[str, Any], actor: str) -> Post:
        with self._lock:
            if self.get(post_id) is None:
                raise KeyError(post_id)
            self._db.execute("UPDATE posts SET body=?, updated_at=? WHERE id=?",
                             (json.dumps(body, default=str), self._ts(), post_id))
            self._event(actor, "post.body", {}, post_id)
            out = self.get(post_id)
        assert out is not None
        return out

    def log_event(self, actor: str, type: str, payload: dict[str, Any], post_id: str | None = None) -> int:
        with self._lock:
            return self._event(actor, type, payload, post_id)

    def last_event_id(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])

    def facts_changed_since_settle(self, action_kinds: Sequence[str] = ()) -> bool:
        """A fact, or an executed action of one of `action_kinds`, changed after the newest forecast or settle."""
        with self._lock:
            base = self._db.execute(
                "SELECT COALESCE(MAX(e.id), 0) FROM events e JOIN posts p ON p.id=e.post_id "
                "WHERE e.type='post.append' AND (p.kind='forecast' OR (p.kind='event' AND p.subject='settled'))"
            ).fetchone()[0]
            marks = ",".join("?" * len(action_kinds))
            row = self._db.execute(
                "SELECT 1 FROM events e JOIN posts p ON p.id=e.post_id WHERE e.id>? AND ("
                "(p.kind='fact' AND e.type IN ('post.append','post.status')) OR "
                "(p.kind='action' AND e.type='post.status' AND json_extract(e.payload,'$.to')='executed' "
                f"AND json_extract(p.body,'$.kind') IN ({marks}))) LIMIT 1", (base, *action_kinds)).fetchone()
            return row is not None

    def latest_event_post(self, subject: str) -> Post | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM posts WHERE kind='event' AND subject=? ORDER BY rowid DESC LIMIT 1",
                (subject,)).fetchone()
            return self._row_to_post(row) if row else None


def MemoryBoard(**kw: Any) -> SqliteBoard:  # noqa: N802
    return SqliteBoard(":memory:", **kw)

"""Fakes so every lane can test without a network."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from gtl.board import MemoryBoard, SqliteBoard
from gtl.contracts import (Context, FileChange, HumanAnswer, InboundMail, LLMResult,
                           OutboundMail, Ports, Post)

__all__ = ["FakeLLM", "FakeFiles", "FakeMailbox", "FakeControlRoom", "FakeClock",
           "MemoryBoard", "SqliteBoard", "make_context"]


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 11, 21, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when

    def advance(self, hours: float = 0, minutes: float = 0, days: float = 0) -> datetime:
        self._now += timedelta(hours=hours, minutes=minutes, days=days)
        return self._now


class FakeLLM:
    """Scripted LLM. responses: role -> list (consumed in order, last repeats) or a callable
    (role, system, user, schema, images) -> str | dict."""

    def __init__(self, responses: dict[str, Any] | Callable[..., Any] | None = None,
                 default: str | dict[str, Any] = "") -> None:
        self.responses = responses if responses is not None else {}
        self.default = default
        self.calls: list[dict[str, Any]] = []
        self._pos: dict[str, int] = {}

    def complete(self, role: str, system: str, user: str, *, schema: dict[str, Any] | None = None,
                 images: tuple[bytes, ...] = (), max_tokens: int = 2000) -> LLMResult:
        self.calls.append({"role": role, "system": system, "user": user, "schema": schema,
                           "images": images, "max_tokens": max_tokens})
        if callable(self.responses):
            out = self.responses(role, system, user, schema, images)
        else:
            seq = self.responses.get(role)
            if callable(seq):
                out = seq(role, system, user, schema, images)
            elif seq:
                i = self._pos.get(role, 0)
                out = seq[min(i, len(seq) - 1)]
                self._pos[role] = i + 1
            else:
                out = self.default
        if isinstance(out, dict):
            return LLMResult(text=json.dumps(out), data=out, model="fake", provider="fake")
        data = None
        if schema is not None:
            from gtl.llm import extract_json
            try:
                data = extract_json(out)
            except ValueError:
                data = None
        return LLMResult(text=out, data=data, model="fake", provider="fake")


class FakeFiles:
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.pending: list[FileChange] = []

    def push(self, *changes: FileChange) -> None:
        self.pending.extend(changes)

    def poll(self) -> list[FileChange]:
        out, self.pending = self.pending, []
        return out

    def read(self, path: str, sha256: str | None = None) -> bytes:
        return self.files[path]

    def local_path(self, path: str, sha256: str | None = None) -> str:
        return path


class FakeMailbox:
    def __init__(self) -> None:
        self.inbound: list[InboundMail] = []
        self.sent: list[OutboundMail] = []
        self._keys: dict[str, str] = {}
        self._threads: dict[str, str] = {}

    def deliver(self, *mails: InboundMail) -> None:
        self.inbound.extend(mails)

    def send(self, mail: OutboundMail) -> str:
        if mail.idempotency_key and mail.idempotency_key in self._keys:
            return self._keys[mail.idempotency_key]
        mid = f"fake-msg-{len(self.sent) + 1}"
        self.sent.append(mail)
        # Like the provider: a reply stays in its thread, a fresh send opens one.
        self._threads[mid] = mail.thread_id or mid
        if mail.idempotency_key:
            self._keys[mail.idempotency_key] = mid
        return mid

    def thread_of(self, message_id: str) -> str | None:
        return self._threads.get(message_id)

    def poll(self) -> list[InboundMail]:
        out, self.inbound = self.inbound, []
        return out


class FakeControlRoom:
    def __init__(self) -> None:
        self.published: list[Post] = []
        self.answers: list[HumanAnswer] = []

    def publish(self, post: Post) -> None:
        self.published.append(post)

    def answer(self, *answers: HumanAnswer) -> None:
        self.answers.extend(answers)

    def poll(self) -> list[HumanAnswer]:
        out, self.answers = self.answers, []
        return out


def make_context(tenant: dict[str, Any] | None = None, agents_playbooks: dict[str, str] | None = None,
                 **overrides: Any) -> Context:
    clock = overrides.pop("clock", None) or FakeClock()
    board = overrides.pop("board", None) or MemoryBoard(now=clock.now)
    ports = overrides.pop("ports", None) or Ports(
        files=FakeFiles(), mail=FakeMailbox(), control_room=FakeControlRoom(), clock=clock)
    llm = overrides.pop("llm", None) or FakeLLM()
    world = overrides.pop("world", "sim")
    return Context(board=board, ports=ports, llm=llm,
                   tenant=tenant if tenant is not None else {"id": "test", "timezone": "Europe/Berlin"},
                   world=world, playbooks=dict(agents_playbooks or {}), **overrides)

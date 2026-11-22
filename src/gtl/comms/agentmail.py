"""The real Mailbox port: AgentMail over REST (docs/design.md D14, D18).

Endpoints used (https://docs.agentmail.to/api-reference):
  POST /v0/inboxes/{inbox}/messages/send
  POST /v0/inboxes/{inbox}/messages/{message}/reply
  GET  /v0/inboxes/{inbox}/threads/{thread}
  GET  /v0/inboxes/{inbox}/messages
  GET  /v0/inboxes/{inbox}/messages/{message}
  GET  /v0/inboxes/{inbox}/messages/{message}/attachments/{attachment}

State lives in one JSON file so a restart neither resends nor re-delivers.
Inbound delivery is at most once: a message is marked delivered when poll
returns it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from gtl.contracts import Clock, InboundMail, OutboundMail

log = logging.getLogger(__name__)

BASE_URL = "https://api.agentmail.to"
RETRIES = 3
BACKOFF_SECONDS = 1.0
# Late deliveries can carry a timestamp older than the newest one seen, so
# each poll looks back a little; message ids keep it exactly-once.
LOOKBACK = timedelta(hours=1)
PAGE_LIMIT = 100


class AgentMailError(RuntimeError):
    """A failed AgentMail call, with the API's own message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"AgentMail {status}: {message}")
        self.status = status


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def strip_html(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    text = "".join(parser.parts)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def _address(sender: str) -> str:
    """`Name <a@b.c>` or `a@b.c` to the bare lowercase address."""
    match = re.search(r"<([^>]+)>", sender)
    return (match.group(1) if match else sender).strip().lower()


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or response.reason_phrase
    if not isinstance(body, dict):
        return str(body)[:300]
    parts = [str(body[k]) for k in ("message", "fix") if body.get(k)]
    if body.get("errors"):
        parts.append(json.dumps(body["errors"])[:300])
    return " | ".join(parts) or str(body.get("name") or response.reason_phrase)


STAND_IN_MARK = "[Stand-in delivery. Intended recipient: "


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now().astimezone()


class AgentMailbox:
    def __init__(
        self,
        tenant: dict[str, Any],
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        state_dir: str | Path = "var",
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
        download_client: httpx.Client | None = None,
        history: bool = False,
    ) -> None:
        self.inboxes: tuple[str, ...] = tuple(tenant["inboxes"].values())
        # Outside addresses are reached only when the tenant says outbound = "live". Until then
        # such mail goes to the stand-in inbox, which must be one of our own.
        outbound = tenant.get("mail") or {}
        self._live = outbound.get("outbound") == "live"
        self._stand_in = str(outbound.get("stand_in") or "").lower() or None
        if self._stand_in and self._stand_in not in {i.lower() for i in self.inboxes}:
            raise ValueError(f"stand-in inbox {self._stand_in!r} is not one of the tenant's inboxes")
        if client is None:
            key = api_key or os.environ.get("AGENTMAIL_API_KEY", "")
            if not key:
                raise ValueError("AGENTMAIL_API_KEY is not set")
            client = httpx.Client(base_url=BASE_URL, timeout=30.0,
                                  headers={"Authorization": f"Bearer {key}"})
        self._client = client
        # Attachment links are signed; they must not carry our API key.
        self._download = download_client or httpx.Client(timeout=30.0)
        self._clock = clock or _SystemClock()
        self._sleep = sleep
        self._state_dir = Path(state_dir)
        self._state_path = self._state_dir / "agentmail-state.json"
        self._lock = threading.Lock()
        self._state = self._load()
        if not self._state_path.exists() and not history:
            # First start: only mail that arrives from now on is delivered.
            started = self._clock.now().astimezone(timezone.utc).isoformat()
            self._state["started_at"] = started
            self._state["inboxes"] = {i: {"cursor": started, "delivered": []} for i in self.inboxes}
            self._save()

    # state

    def _load(self) -> dict[str, Any]:
        state: dict[str, Any] = {"sent": {}, "threads": {}, "inboxes": {}}
        if self._state_path.exists():
            state.update(json.loads(self._state_path.read_text(encoding="utf-8")))
        return state

    def _save(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._state_dir, prefix=".agentmail-state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._state_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def thread_of(self, message_id: str) -> str | None:
        """Thread of a message we sent or delivered, or None if unknown.

        AgentMail thread ids are per inbox: one conversation has a different
        id in the sender's and the receiver's inbox. For mail between our own
        inboxes the id stored for a sent message is the one the API reported,
        which can be the recipient's.
        """
        return self._state["threads"].get(message_id)

    # http

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(RETRIES + 1):
            last = attempt == RETRIES
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if last:
                    raise AgentMailError(0, f"network error: {type(exc).__name__}") from exc
                self._sleep(BACKOFF_SECONDS * 2 ** attempt)
                continue
            if (response.status_code == 429 or response.status_code >= 500) and not last:
                self._sleep(BACKOFF_SECONDS * 2 ** attempt)
                continue
            if response.status_code >= 400:
                raise AgentMailError(response.status_code, _error_text(response))
            return response.json()
        raise AssertionError("unreachable")

    # send

    def send(self, mail: OutboundMail) -> str:
        # Thread ids are per inbox, and send records the id as the API reports
        # it. A reply must pass the thread_id of the inbound mail as seen by
        # the receiving inbox (the one named in mail.inbox).
        if mail.inbox not in self.inboxes:
            raise ValueError(f"{mail.inbox!r} is not one of the tenant's inboxes")
        with self._lock:
            key = mail.idempotency_key
            if key and key in self._state["sent"]:
                return self._state["sent"][key]["message_id"]
            # The API accepts only A-Za-z0-9-._~ in this header, so send a digest.
            headers = {"Idempotency-Key": hashlib.sha256(key.encode()).hexdigest()} if key else {}
            to, text = self._recipient(mail)
            body: dict[str, Any] = {"to": [to], "text": text}
            inbox = mail.inbox
            if mail.thread_id:
                thread = self._request("GET", f"/v0/inboxes/{inbox}/threads/{mail.thread_id}")
                last = thread["last_message_id"]
                result = self._request("POST", f"/v0/inboxes/{inbox}/messages/{last}/reply",
                                       json=body, headers=headers)
            else:
                body["subject"] = mail.subject
                result = self._request("POST", f"/v0/inboxes/{inbox}/messages/send",
                                       json=body, headers=headers)
            message_id, thread_id = result["message_id"], result["thread_id"]
            self._state["threads"][message_id] = thread_id
            if key:
                self._state["sent"][key] = {
                    "message_id": message_id, "thread_id": thread_id, "inbox": inbox,
                    "action_id": mail.meta.get("action_id"), "to": to, "intended_to": mail.to,
                    "sent_at": self._clock.now().isoformat()}
            self._save()
            return message_id

    def _recipient(self, mail: OutboundMail) -> tuple[str, str]:
        """Where the mail really goes, and its text. Our own inboxes are always reachable."""
        if self._live or _address(mail.to) in {i.lower() for i in self.inboxes}:
            return mail.to, mail.body
        if not self._stand_in:
            raise ValueError(f"outbound mail is not live and no stand-in inbox is set; not sending to {mail.to}")
        return self._stand_in, f"{STAND_IN_MARK}{mail.to}]\n\n{mail.body}"

    # poll

    def poll(self) -> list[InboundMail]:
        """New received mail across all inboxes, oldest first.

        An inbox that fails is skipped and tried again next poll; the error
        is raised only when no inbox delivered anything.
        """
        with self._lock:
            found: list[tuple[str, InboundMail]] = []
            error: Exception | None = None
            for inbox in self.inboxes:
                try:
                    found.extend(self._poll_inbox(inbox))
                except (AgentMailError, OSError) as exc:
                    log.warning("polling %s failed: %s", inbox, exc)
                    error = error or exc
            if not found and error:
                raise error
            if found:
                self._save()
            found.sort(key=lambda pair: _when(pair[0]))
            return [mail for _, mail in found]

    def _poll_inbox(self, inbox: str) -> list[tuple[str, InboundMail]]:
        entry = self._state["inboxes"].get(inbox) or {"cursor": None, "delivered": []}
        delivered = set(entry["delivered"])
        params: dict[str, Any] = {"ascending": "true", "limit": PAGE_LIMIT}
        if entry["cursor"]:
            after = datetime.fromisoformat(entry["cursor"]) - LOOKBACK
            # The API rejects "+00:00" offsets in query datetimes; it wants UTC with Z.
            params["after"] = after.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        items: list[dict[str, Any]] = []
        while True:
            page = self._request("GET", f"/v0/inboxes/{inbox}/messages", params=params)
            items.extend(page["messages"])
            if not page.get("next_page_token"):
                break
            params["page_token"] = page["next_page_token"]
        out: list[tuple[str, InboundMail]] = []
        started = self._state.get("started_at")
        newest = entry["cursor"]
        for item in sorted(items, key=lambda i: _when(i["timestamp"])):
            if newest is None or _when(item["timestamp"]) > _when(newest):
                newest = item["timestamp"]
            if started and _when(item["timestamp"]) < _when(started):
                continue
            if item["message_id"] in delivered or not self._is_received(inbox, item):
                continue
            mail = self._read(inbox, item["message_id"])
            if mail.body.startswith(STAND_IN_MARK):
                delivered.add(item["message_id"])  # our own mail in the stand-in inbox is not an answer
                continue
            out.append((item["timestamp"], mail))
        # Commit only after every message was read, so a failure loses nothing.
        for _, mail in out:
            self._state["threads"][mail.message_id] = mail.thread_id
        self._state["inboxes"][inbox] = {
            "cursor": newest, "delivered": sorted(delivered | {m.message_id for _, m in out})}
        return out

    @staticmethod
    def _is_received(inbox: str, item: dict[str, Any]) -> bool:
        """Mail between our own inboxes counts, so only the labels and the
        inbox's own address tell a received message from a sent one."""
        labels = set(item.get("labels") or ())
        if labels & {"sent", "draft"}:
            return False
        if "received" in labels:
            return True
        return _address(item["from"]) != inbox.lower()

    def _read(self, inbox: str, message_id: str) -> InboundMail:
        msg = self._request("GET", f"/v0/inboxes/{inbox}/messages/{message_id}")
        body = msg.get("text") or strip_html(msg.get("html") or "")
        return InboundMail(
            message_id=message_id, thread_id=msg["thread_id"], inbox=inbox,
            sender=msg["from"], subject=msg.get("subject") or "", body=body,
            received_at=msg["timestamp"],
            attachments=self._download_attachments(inbox, msg))

    def _download_attachments(self, inbox: str, msg: dict[str, Any]) -> tuple[str, ...]:
        paths: list[str] = []
        message_id = msg["message_id"]
        folder = self._state_dir / "mail-attachments" / re.sub(r"[^\w.@-]", "_", message_id)
        for att in msg.get("attachments") or ():
            meta = self._request(
                "GET", f"/v0/inboxes/{inbox}/messages/{message_id}/attachments/{att['attachment_id']}")
            response = self._download.get(meta["download_url"])
            if response.status_code >= 400:
                raise AgentMailError(response.status_code, "attachment download failed")
            name = Path(att.get("filename") or att["attachment_id"]).name or att["attachment_id"]
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / name
            target.write_bytes(response.content)
            paths.append(str(target))
        return tuple(paths)

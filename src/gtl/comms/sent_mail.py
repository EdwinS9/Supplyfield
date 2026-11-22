"""Sync provider-confirmed outgoing messages to a reviewable private archive."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import httpx

from gtl.comms.agentmail import BASE_URL, PAGE_LIMIT, STAND_IN_MARK
from gtl.config import load_env, load_tenant

Record = dict[str, Any]


def _record_id(inbox: str, message_id: str) -> str:
    """Same key as the original archive, independent of recipient-side copies."""
    return hashlib.sha256(f"{inbox}\0{message_id}".encode()).hexdigest()


def _sent_ids(client: httpx.Client, path: str) -> Iterator[str]:
    params = {"labels": "sent", "include_trash": "true", "limit": str(PAGE_LIMIT)}
    seen: set[str] = set()
    while True:
        page = client.get(path, params=params).raise_for_status().json()
        for message in page["messages"]:
            if "sent" in message["labels"] and "draft" not in message["labels"]:
                yield message["message_id"]
        token = page.get("next_page_token")
        if not token:
            return
        if token in seen:
            raise ValueError("AgentMail repeated a page token; sync aborted")
        seen.add(token)
        params["page_token"] = token


def _record(msg: Record, inboxes: set[str], stand_in: str, previous: Record) -> Record:
    record = {**previous, **{k: msg[k] for k in ("inbox_id", "message_id", "thread_id", "timestamp", "from", "to")}}
    body = msg.get("text") or msg.get("html") or ""
    recipients = {parseaddr(address)[1].casefold() for f in ("to", "cc", "bcc") for address in msg.get(f) or ()}
    route = "unknown" if not recipients else "internal" if recipients <= inboxes else "external"
    intended = previous.get("intended_recipient")
    lead = body.partition("\n")[0].rstrip("\r")
    if stand_in in inboxes and recipients == {stand_in} and lead.startswith(STAND_IN_MARK) and lead.endswith("]"):
        intended = lead[len(STAND_IN_MARK):-1]
        route = "stand-in"
    record.update(subject=msg.get("subject") or "", cc=msg.get("cc") or [], bcc=msg.get("bcc") or [],
                  route=route, intended_recipient=intended, body=body,
                  body_format="html" if not msg.get("text") and msg.get("html") else "text")
    return record


def _cell(value: str) -> str:
    return "<code>" + html.escape(" ".join(value.split())).replace("|", "&#124;") + "</code>"


def _index(records: dict[str, Record], inboxes: list[str], started: str) -> str:
    full = sum(r.get("body") is not None for r in records.values())
    lines = ["# Sent messages", "", f"Provider scan started at {started}. Inboxes: {', '.join(map(_cell, inboxes))}.",
             "", f"**{len(records)}** verified sent messages. Transcripts imported: **{full}/{len(records)}**.", "",
             "| Sent at | Route | Recipient (To) | Subject | Record |", "|---|---|---|---|---|"]
    for key, msg in sorted(records.items(), key=lambda item: item[1]["timestamp"], reverse=True):
        row = [msg["timestamp"], msg["route"], ", ".join(msg["to"]), msg["subject"]]
        lines.append("| " + " | ".join(map(_cell, row)) + f" | [Open]({key}.json) |")
    return "\n".join(lines) + "\n"


def sync(tenant: dict[str, Any], folder: Path, client: httpx.Client) -> int:
    """Backfill missing sends and bodies. Keep reviewed records and reconciliation evidence."""
    inboxes = list(dict.fromkeys(tenant["inboxes"].values()))
    if not inboxes:
        raise ValueError("No tenant inboxes configured")
    own = {i.casefold() for i in inboxes}
    stand_in = str(tenant.get("mail", {}).get("stand_in") or "").casefold()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")}
    updates = {}
    for inbox in inboxes:
        path = f"/v0/inboxes/{quote(inbox, safe='')}/messages"
        for mid in _sent_ids(client, path):
            key = _record_id(inbox, mid)
            previous = records.get(key, {})
            if key in updates or previous.get("body") is not None:
                continue
            msg = client.get(f"{path}/{quote(mid, safe='')}").raise_for_status().json()
            if (msg["inbox_id"] != inbox or msg["message_id"] != mid
                    or "sent" not in msg["labels"] or "draft" in msg["labels"]):
                raise ValueError("Inconsistent provider message; sync aborted")
            updates[key] = _record(msg, own, stand_in, previous)

    # A failed provider scan must not publish partial results as the current index.
    index = _index(records | updates, inboxes, started)
    folder.mkdir(exist_ok=True, parents=True)
    for key, record in updates.items():
        (folder / f"{key}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (folder / "README.md").write_text(index, encoding="utf-8")
    return len(updates)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="Tenant folder name under tenants/")
    args = parser.parse_args()
    if not args.tenant or Path(args.tenant).name != args.tenant or args.tenant in (".", ".."):
        parser.error("--tenant must be a single folder name")
    load_env()
    key = os.getenv("AGENTMAIL_API_KEY")
    if not key:
        parser.exit(2, "AGENTMAIL_API_KEY is missing. No scan performed.\n")
    folder = Path("data/sent-mail") / args.tenant
    with httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {key}"}, timeout=30) as client:
        count = sync(load_tenant(args.tenant), folder, client)
    print(f"{count} new or completed sent records. Review {folder} before publishing.")


if __name__ == "__main__":
    main()

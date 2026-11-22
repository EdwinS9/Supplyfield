"""Structured view of agent email traffic: outbound actions, inbound mail and optional live provider data."""
from __future__ import annotations

import json
import re
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from gtl.comms.agentmail import PAGE_LIMIT, STAND_IN_MARK, strip_html
from gtl.contracts import LLM
from gtl.govern.executor import COMMITMENTS

MAX_PROVIDER_PAGES = 2
_ADDR = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
FAST_TRANSLATE_MODELS = ("MiniCPM5-2B", "DeepSeek-V4-Flash", "MiniCPM-V-4.7-35B-A3B")

TRANSLATE_SYSTEM = (
    "Translate the following business email into {target_language}. "
    "Preserve every number, quantity, date, currency amount, purchase order number, part code, and reference ID exactly. "
    "Reply in this exact plain-text format and nothing else:\n"
    "Source: <original language name in English, e.g. Polish, German, Korean, Chinese, English>\n"
    "Subject: <translated subject>\n"
    "\n"
    "<translated body>"
)


def _bare(value: Any) -> str:
    text = str(value or "").strip()
    _, addr = parseaddr(text)
    if addr and "@" in addr:
        return addr.lower()
    match = _ADDR.search(text)
    return match.group(0).lower() if match else text.lower()


def _preview(text: str | None, limit: int = 180) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _extract_stand_in(text: str | None) -> tuple[str | None, str]:
    """Strip the stand-in header line when present and return (intended_recipient, cleaned_text)."""
    raw = str(text or "")
    if raw.startswith(STAND_IN_MARK):
        end = raw.find("]")
        if end != -1:
            return raw[len(STAND_IN_MARK):end].strip(), raw[end + 1:].lstrip(" \t\r\n")
    return None, raw


def _suppliers(board: Any) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Return (by_id, by_email) maps built from active supplier facts on the Board."""
    by_id: dict[str, dict[str, str]] = {}
    for fact in board.facts("supplier"):
        if fact.field in ("name", "contact_email", "language") and fact.value:
            by_id.setdefault(fact.key, {"id": fact.key})[fact.field] = str(fact.value)
    by_email: dict[str, dict[str, str]] = {}
    for info in by_id.values():
        email = _bare(info.get("contact_email"))
        if email:
            by_email[email] = info
    return by_id, by_email


def _inbox_map(tenant: dict[str, Any] | None) -> tuple[list[dict[str, str]], dict[str, str]]:
    raw = (tenant or {}).get("inboxes") or {}
    items = [{"role": str(role), "address": str(addr)} for role, addr in raw.items() if isinstance(addr, str)]
    by_addr = {_bare(item["address"]): item["role"] for item in items}
    return items, by_addr


def _agentmail_state(var_dir: str | Path) -> dict[str, Any]:
    path = Path(var_dir) / "agentmail-state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _claims_for(board: Any, post_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for dep_id in board.dependents(post_id):
        post = board.get(dep_id)
        if post is None or post.kind != "claim":
            continue
        b = post.body
        evidence = b.get("evidence") or ()
        quote_text = next((e.get("quote") for e in evidence if isinstance(e, dict) and e.get("quote")), None)
        out.append({
            "post_id": post.id,
            "status": post.status,
            "entity": b.get("entity"),
            "key": b.get("key"),
            "field": b.get("field"),
            "value": b.get("value"),
            "quote": quote_text,
        })
    return out


def _board_messages(
    board: Any,
    tenant: dict[str, Any] | None,
    state: dict[str, Any],
    by_id: dict[str, dict[str, str]],
    by_email: dict[str, dict[str, str]],
    inbox_roles: dict[str, str],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    sent_by_mid = {
        str(v["message_id"]): v
        for v in (state.get("sent") or {}).values()
        if isinstance(v, dict) and v.get("message_id")
    }
    threads_map = state.get("threads") if isinstance(state.get("threads"), dict) else {}
    mail_cfg = (tenant or {}).get("mail") or {}
    outbound_live = mail_cfg.get("outbound") == "live"
    own_addrs = set(inbox_roles)

    for action in board.query(kind="action", limit=-1):
        body = action.body
        kind = str(body.get("kind") or "")
        if kind != "send_email" and kind not in COMMITMENTS:
            continue
        params = body.get("params") or {}
        sent_mail = body.get("sent_mail") if isinstance(body.get("sent_mail"), dict) else {}
        message_id = body.get("message_id")
        if kind in COMMITMENTS and not message_id and not sent_mail:
            continue

        state_row = sent_by_mid.get(str(message_id), {}) if message_id else {}
        supplier_id = str(params.get("to_id") or params.get("supplier_id") or "") or None
        sup = by_id.get(supplier_id or "") or {}
        inbox = str(sent_mail.get("inbox") or params.get("inbox") or state_row.get("inbox")
                    or ((tenant or {}).get("inboxes") or {}).get("orders" if kind in COMMITMENTS else "supply") or "")
        intended_to = str(sent_mail.get("to") or params.get("to") or state_row.get("intended_to")
                          or sup.get("contact_email") or "")
        actual_to = str(state_row.get("to") or intended_to)
        if not sup and intended_to:
            sup = by_email.get(_bare(intended_to)) or {}
            supplier_id = supplier_id or sup.get("id")

        subject = str(sent_mail.get("subject") or params.get("subject") or body.get("reason") or kind.replace("_", " "))
        text_body = sent_mail.get("body") if sent_mail.get("body") is not None else params.get("body")
        body_en = params.get("body_en")
        thread_id = (sent_mail.get("thread_id") or params.get("thread_id")
                     or state_row.get("thread_id") or (threads_map.get(str(message_id)) if message_id else None))

        route = sent_mail.get("route")
        if not route:
            if action.status != "executed":
                route = "pending" if action.status in ("requested", "allowed", "needs_approval", "approved") else "not_sent"
            elif _bare(intended_to) in own_addrs:
                route = "internal"
            elif outbound_live or (actual_to and _bare(actual_to) == _bare(intended_to) and _bare(actual_to) not in own_addrs):
                route = "live"
            else:
                route = "stand-in"

        question_ids = [str(q) for q in (params.get("question_ids") or ([params["question_id"]] if params.get("question_id") else []))]
        verdict = body.get("verdict") if isinstance(body.get("verdict"), dict) else None
        messages.append({
            "id": action.id,
            "direction": "outbound",
            "source": "board",
            "timestamp": str(body.get("executed_at") or state_row.get("sent_at") or action.created_at),
            "inbox": inbox,
            "role": inbox_roles.get(_bare(inbox), ""),
            "from": inbox,
            "to": actual_to or intended_to,
            "intended_to": intended_to if intended_to and _bare(intended_to) != _bare(actual_to) else None,
            "counterparty": sup.get("name") or intended_to or actual_to or "Unknown recipient",
            "supplier_id": supplier_id,
            "subject": subject,
            "preview": _preview(body_en or text_body or body.get("reason")),
            "body": text_body,
            "body_en": body_en if body_en and body_en != text_body else None,
            "has_body": text_body is not None or body_en is not None,
            "language": params.get("language") or sup.get("language"),
            "status": action.status,
            "route": route,
            "action_kind": kind,
            "agent": str(body.get("requested_by") or action.author),
            "reason": body.get("reason"),
            "verdict": verdict,
            "error": body.get("error"),
            "message_id": str(message_id) if message_id else None,
            "thread_id": str(thread_id) if thread_id else None,
            "post_id": action.id,
            "question_ids": question_ids,
            "attachments": [],
            "claims": [],
        })

    for post in board.query(kind="event", subject_prefix="mail.received", limit=-1):
        b = post.body
        inbox = str(b.get("inbox") or "")
        sender = str(b.get("sender") or "")
        sup = by_email.get(_bare(sender)) or {}
        text_body = str(b.get("body") or "")
        attachments = [Path(str(a)).name for a in (b.get("attachments") or ())]
        messages.append({
            "id": post.id,
            "direction": "inbound",
            "source": "board",
            "timestamp": str(b.get("received_at") or post.created_at),
            "inbox": inbox,
            "role": inbox_roles.get(_bare(inbox), ""),
            "from": sender,
            "to": inbox,
            "intended_to": None,
            "counterparty": sup.get("name") or sender or "Unknown sender",
            "supplier_id": sup.get("id"),
            "subject": str(b.get("subject") or "(no subject)"),
            "preview": _preview(text_body),
            "body": text_body,
            "body_en": None,
            "has_body": True,
            "language": sup.get("language"),
            "status": "received",
            "route": "inbound",
            "action_kind": None,
            "agent": "mail_reader",
            "reason": None,
            "verdict": None,
            "error": None,
            "message_id": str(b.get("message_id") or "") or None,
            "thread_id": str(b.get("thread_id") or "") or None,
            "post_id": post.id,
            "question_ids": [],
            "attachments": attachments,
            "claims": _claims_for(board, post.id),
        })

    return messages


def _provider_messages(
    client: httpx.Client,
    tenant: dict[str, Any] | None,
    by_email: dict[str, dict[str, str]],
    inbox_roles: dict[str, str],
) -> tuple[list[dict[str, Any]], str | None]:
    inboxes = list(dict.fromkeys(addr for addr in ((tenant or {}).get("inboxes") or {}).values() if isinstance(addr, str)))
    if not inboxes:
        return [], None
    own_addrs = set(inbox_roles)
    stand_in = _bare(((tenant or {}).get("mail") or {}).get("stand_in"))
    found: list[dict[str, Any]] = []
    errors: list[str] = []

    for inbox in inboxes:
        path = f"/v0/inboxes/{quote(inbox, safe='')}/messages"
        params: dict[str, str] = {"limit": str(PAGE_LIMIT)}
        seen_tokens: set[str] = set()
        pages = 0
        try:
            while pages < MAX_PROVIDER_PAGES:
                resp = client.get(path, params=params)
                resp.raise_for_status()
                payload = resp.json()
                for item in payload.get("messages") or ():
                    labels = set(item.get("labels") or ())
                    if "draft" in labels:
                        continue
                    is_sent = "sent" in labels or (_bare(item.get("from")) == _bare(inbox) and "received" not in labels)
                    preview_raw = str(item.get("preview") or "")
                    intended, clean_preview = _extract_stand_in(preview_raw)
                    # Skip the duplicate inbound echo in the stand-in inbox of our own redirected mail.
                    if not is_sent and stand_in and _bare(inbox) == stand_in and intended is not None:
                        continue
                    to_list = [str(a) for a in (item.get("to") or ())]
                    to_str = ", ".join(to_list)
                    recipients = {_bare(a) for a in to_list if a}
                    if is_sent:
                        if stand_in and recipients == {stand_in} and intended is not None:
                            route = "stand-in"
                        elif recipients and recipients <= own_addrs:
                            route = "internal"
                        else:
                            route = "live"
                        counter_addr = intended or (to_list[0] if to_list else "")
                    else:
                        route = "inbound"
                        counter_addr = str(item.get("from") or "")
                    sup = by_email.get(_bare(counter_addr)) or {}
                    found.append({
                        "id": f"provider:{inbox}:{item['message_id']}",
                        "direction": "outbound" if is_sent else "inbound",
                        "source": "provider",
                        "timestamp": str(item.get("timestamp") or item.get("created_at") or ""),
                        "inbox": inbox,
                        "role": inbox_roles.get(_bare(inbox), ""),
                        "from": str(item.get("from") or (inbox if is_sent else "")),
                        "to": to_str or ("" if is_sent else inbox),
                        "intended_to": intended,
                        "counterparty": sup.get("name") or counter_addr or "Unknown",
                        "supplier_id": sup.get("id"),
                        "subject": str(item.get("subject") or "(no subject)"),
                        "preview": _preview(clean_preview),
                        "body": None,
                        "body_en": None,
                        "has_body": False,
                        "language": sup.get("language"),
                        "status": "executed" if is_sent else "received",
                        "route": route,
                        "action_kind": None,
                        "agent": None,
                        "reason": None,
                        "verdict": None,
                        "error": None,
                        "message_id": str(item["message_id"]),
                        "thread_id": str(item.get("thread_id") or "") or None,
                        "post_id": None,
                        "question_ids": [],
                        "attachments": [str(a.get("filename") or a.get("attachment_id") or "attachment")
                                        for a in (item.get("attachments") or ()) if isinstance(a, dict)],
                        "claims": [],
                    })
                token = payload.get("next_page_token")
                if not token or token in seen_tokens:
                    break
                seen_tokens.add(token)
                params["page_token"] = str(token)
                pages += 1
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            errors.append(f"{inbox}: {exc}")

    return found, ("; ".join(errors) if errors else None)


def _merge(board_msgs: list[dict[str, Any]], provider_msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    merged: list[dict[str, Any]] = []
    for msg in board_msgs:
        merged.append(msg)
        if msg.get("message_id"):
            by_key[(msg["direction"], msg["message_id"])] = msg

    for pmsg in provider_msgs:
        existing = by_key.get((pmsg["direction"], pmsg["message_id"])) if pmsg.get("message_id") else None
        if existing is not None:
            existing["source"] = "board+provider"
            existing["thread_id"] = existing["thread_id"] or pmsg["thread_id"]
            if pmsg["route"] in ("stand-in", "internal", "live"):
                existing["route"] = pmsg["route"]
            if pmsg["intended_to"] and not existing["intended_to"]:
                existing["intended_to"] = pmsg["intended_to"]
            if not existing["preview"] and pmsg["preview"]:
                existing["preview"] = pmsg["preview"]
            if not existing["attachments"] and pmsg["attachments"]:
                existing["attachments"] = pmsg["attachments"]
        else:
            merged.append(pmsg)
            if pmsg.get("message_id"):
                by_key[(pmsg["direction"], pmsg["message_id"])] = pmsg

    merged.sort(key=lambda m: m.get("timestamp") or "", reverse=True)
    return merged


def _group_threads(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    q_to_bucket: dict[str, str] = {}

    # Walk oldest first so thread anchors stay stable.
    for msg in sorted(messages, key=lambda m: m.get("timestamp") or ""):
        key = None
        if msg.get("thread_id"):
            key = f"thread:{msg['thread_id']}"
        else:
            for qid in msg.get("question_ids") or ():
                if qid in q_to_bucket:
                    key = q_to_bucket[qid]
                    break
            if key is None:
                key = f"msg:{msg['id']}"
        buckets.setdefault(key, []).append(msg)
        for qid in msg.get("question_ids") or ():
            q_to_bucket.setdefault(qid, key)

    threads: list[dict[str, Any]] = []
    for key, items in buckets.items():
        items.sort(key=lambda m: m.get("timestamp") or "")
        first, latest = items[0], items[-1]
        outbound = sum(1 for m in items if m["direction"] == "outbound")
        inbound = sum(1 for m in items if m["direction"] == "inbound")
        pending = sum(1 for m in items if m["status"] in ("requested", "allowed", "needs_approval", "approved"))
        issues = sum(1 for m in items if m["status"] in ("blocked", "rejected", "failed"))
        counterparties = list(dict.fromkeys(m["counterparty"] for m in items if m.get("counterparty")))
        threads.append({
            "id": key,
            "thread_id": latest.get("thread_id") or first.get("thread_id"),
            "subject": first.get("subject") or latest.get("subject") or "(no subject)",
            "counterparty": counterparties[0] if counterparties else "Unknown",
            "inbox": latest.get("inbox") or first.get("inbox") or "",
            "role": latest.get("role") or first.get("role") or "",
            "latest_at": latest.get("timestamp") or "",
            "started_at": first.get("timestamp") or "",
            "message_count": len(items),
            "outbound_count": outbound,
            "inbound_count": inbound,
            "pending_count": pending,
            "issue_count": issues,
            "latest_status": latest.get("status"),
            "latest_direction": latest.get("direction"),
            "latest_route": latest.get("route"),
            "latest_preview": latest.get("preview") or "",
            "messages": items,
        })
    threads.sort(key=lambda t: t["latest_at"], reverse=True)
    return threads


def mail_view(
    board: Any,
    tenant: dict[str, Any] | None,
    *,
    var_dir: str | Path = "var",
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """All agent email traffic from the Board, optionally enriched with live AgentMail inbox records."""
    inboxes, inbox_roles = _inbox_map(tenant)
    by_id, by_email = _suppliers(board)
    state = _agentmail_state(var_dir)
    board_msgs = _board_messages(board, tenant, state, by_id, by_email, inbox_roles)
    provider_msgs: list[dict[str, Any]] = []
    provider_error: str | None = None
    if client is not None:
        provider_msgs, provider_error = _provider_messages(client, tenant, by_email, inbox_roles)

    messages = _merge(board_msgs, provider_msgs)
    threads = _group_threads(messages)
    outbound_mode = str(((tenant or {}).get("mail") or {}).get("outbound") or "stand_in")
    summary = {
        "total": len(messages),
        "threads": len(threads),
        "outbound": sum(1 for m in messages if m["direction"] == "outbound"),
        "inbound": sum(1 for m in messages if m["direction"] == "inbound"),
        "sent": sum(1 for m in messages if m["direction"] == "outbound" and m["status"] == "executed"),
        "pending": sum(1 for m in messages if m["status"] in ("requested", "allowed", "needs_approval", "approved")),
        "issues": sum(1 for m in messages if m["status"] in ("blocked", "rejected", "failed")),
    }
    return {
        "outbound_mode": outbound_mode,
        "provider_connected": client is not None and provider_error is None,
        "provider_error": provider_error,
        "inboxes": inboxes,
        "summary": summary,
        "threads": threads,
        "messages": messages,
    }


def mail_message(
    board: Any,
    tenant: dict[str, Any] | None,
    *,
    inbox: str,
    message_id: str,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """Full body and metadata for one email, from the Board first or from AgentMail on demand."""
    for post in board.query(kind="event", subject_prefix="mail.received", limit=-1):
        if str(post.body.get("message_id") or "") == message_id:
            return {
                "message_id": message_id,
                "inbox": str(post.body.get("inbox") or inbox),
                "subject": str(post.body.get("subject") or ""),
                "from": str(post.body.get("sender") or ""),
                "to": str(post.body.get("inbox") or inbox),
                "body": str(post.body.get("body") or ""),
                "body_en": None,
                "intended_to": None,
                "attachments": [Path(str(a)).name for a in (post.body.get("attachments") or ())],
                "claims": _claims_for(board, post.id),
            }
    for action in board.query(kind="action", limit=-1):
        if str(action.body.get("message_id") or "") == message_id:
            sent_mail = action.body.get("sent_mail") if isinstance(action.body.get("sent_mail"), dict) else {}
            params = action.body.get("params") or {}
            body_text = sent_mail.get("body") if sent_mail.get("body") is not None else params.get("body")
            if body_text is not None or params.get("body_en") is not None:
                return {
                    "message_id": message_id,
                    "inbox": str(sent_mail.get("inbox") or params.get("inbox") or inbox),
                    "subject": str(sent_mail.get("subject") or params.get("subject") or ""),
                    "from": str(sent_mail.get("inbox") or params.get("inbox") or inbox),
                    "to": str(sent_mail.get("to") or params.get("to") or ""),
                    "body": str(body_text or ""),
                    "body_en": params.get("body_en"),
                    "intended_to": None,
                    "attachments": [],
                    "claims": [],
                }

    if client is None:
        return None
    allowed = {addr for addr in ((tenant or {}).get("inboxes") or {}).values() if isinstance(addr, str)}
    if inbox not in allowed:
        return None
    path = f"/v0/inboxes/{quote(inbox, safe='')}/messages/{quote(message_id, safe='')}"
    try:
        resp = client.get(path)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        msg = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    raw_body = msg.get("text") or strip_html(msg.get("html") or "")
    intended, clean_body = _extract_stand_in(raw_body)
    return {
        "message_id": str(msg.get("message_id") or message_id),
        "inbox": str(msg.get("inbox_id") or inbox),
        "subject": str(msg.get("subject") or ""),
        "from": str(msg.get("from") or ""),
        "to": ", ".join(str(a) for a in (msg.get("to") or ())),
        "body": clean_body,
        "body_en": None,
        "intended_to": intended,
        "attachments": [str(a.get("filename") or a.get("attachment_id") or "attachment")
                        for a in (msg.get("attachments") or ()) if isinstance(a, dict)],
        "claims": [],
    }


def parse_translation(raw: str, fallback_subject: str, target_language: str, model: str = "") -> dict[str, Any]:
    """Parse plain-text `Source:` / `Subject:` + body translation output."""
    text = raw.strip()
    if not text:
        raise ValueError("Model returned an empty translation")
    source_language = "Unknown"
    subject = fallback_subject
    lines = text.splitlines()
    idx = 0
    while idx < len(lines) and idx < 4:
        line = lines[idx].strip()
        low = line.lower()
        if low.startswith("source:"):
            source_language = line.split(":", 1)[1].strip() or "Unknown"
            idx += 1
        elif low.startswith("subject:"):
            subject = line.split(":", 1)[1].strip() or fallback_subject
            idx += 1
        elif not line and idx > 0:
            idx += 1
            break
        else:
            break
    body = "\n".join(lines[idx:]).strip()
    if not body:
        body = text
    return {
        "translated": True,
        "target_language": target_language,
        "source_language": source_language,
        "already_in_target": source_language.casefold() == target_language.casefold(),
        "subject": subject,
        "body": body,
        "model": model,
    }


def translate_mail(
    subject: str,
    body: str,
    llm: LLM,
    *,
    target_language: str = "English",
) -> dict[str, Any]:
    """Translate an email subject and body on the spot via the configured LLM."""
    system = TRANSLATE_SYSTEM.format(target_language=target_language)
    user = f"Subject: {subject}\n\n{body}".strip()
    result = llm.complete("reader", system, user, max_tokens=1000)
    if isinstance(result.data, dict) and isinstance(result.data.get("body"), str) and result.data["body"].strip():
        source_language = str(result.data.get("source_language") or "").strip() or "Unknown"
        return {
            "translated": True,
            "target_language": target_language,
            "source_language": source_language,
            "already_in_target": bool(result.data.get("already_in_target", source_language.casefold() == target_language.casefold())),
            "subject": str(result.data.get("subject") or subject).strip(),
            "body": result.data["body"].strip(),
            "model": result.model,
        }
    return parse_translation(result.text, subject, target_language, model=result.model)

"""Where the numbers on the screens come from, so every screen can say it (B25).

The tenant config states whether its sources are the customer's real files or a fictional sample.
The Board gives the run: when the agents started and when they last acted. A hosted snapshot may add
a one-line description of the run in its manifest, and name one notice on the Board to lead with:
the decision a person has to take, with the mails it rests on.
"""

import json
from pathlib import Path
from typing import Any

KINDS = ("real", "fictional")
INDEX = "portal-current.json"
MANIFEST = "snapshot.json"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _lead(board: Any, post_id: Any) -> dict | None:
    """The notice a snapshot leads with, and the received mails among its evidence."""
    post = board.get(str(post_id)) if post_id else None
    if post is None:
        return None
    body = post.body
    title, detail, role = body.get("title"), body.get("detail"), body.get("for_role")
    if post.kind == "decision":
        # A decision card has no title: its problem states it, and the recommended option is the ask.
        problem = str(body.get("problem") or "")
        options, pick = body.get("options") or [], body.get("recommended")
        ask = options[pick].get("title") if isinstance(pick, int) and 0 <= pick < len(options) else None
        title = title or problem.split(". ", 1)[0].rstrip(".")
        detail = problem + (f" Recommended: {ask}." if ask else "")
        role = body.get("approver_role")
    if not title:
        return None
    mails = []
    for ref in body.get("evidence_post_ids") or []:
        source = board.get(str(ref))
        if source is not None and source.kind == "event" and source.subject == "mail.received":
            mails.append({"post_id": source.id, "sender": source.body.get("sender"),
                          "at": source.body.get("received_at") or source.created_at})
    return {"post_id": post.id, "kind": post.kind, "status": post.status, "title": title, "detail": detail,
            "for_role": role, "at": post.created_at, "mails": mails}


def provenance_view(board: Any, tenant: dict | None, var_dir: Path) -> dict:
    """The data source of everything the control room shows: real, fictional or not stated."""
    tenant = tenant or {}
    kind = (tenant.get("provenance") or {}).get("kind")
    index = _read_json(var_dir / INDEX)
    manifest = _read_json(var_dir / MANIFEST)
    first = board.query(limit=1)
    last_event = board.events_since(max(board.last_event_id() - 1, 0))
    forecasts = board.query(kind="forecast", status="current")
    # One notice id, or several: the first leads, the others follow it.
    named = manifest.get("lead_post") if isinstance(manifest, dict) else None
    leads = [lead for ref in ([named] if isinstance(named, str) else named or []) if (lead := _lead(board, ref))]
    return {
        "kind": kind if kind in KINDS else "unstated",
        "tenant_name": tenant.get("name") or tenant.get("id"),
        "source": (tenant.get("sources") or {}).get("data_root"),
        "timezone": tenant.get("timezone"),
        "files": len(index) if isinstance(index, dict) else None,
        "started_at": first[0].created_at if first else None,
        "last_activity_at": last_event[-1]["ts"] if last_event else None,
        "forecast_at": forecasts[-1].created_at if forecasts else None,
        "run": manifest.get("run") if isinstance(manifest, dict) else None,
        "lead": leads[0] if leads else None,
        "more": leads[1:],
    }

"""Ready-made listener handlers. Any ``callable(ChangeEvent)`` works too."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Callable, TextIO

import httpx

from .events import ChangeEvent

log = logging.getLogger("soony_api.handlers")


def console(stream: TextIO | None = None, *, details: bool = True) -> Callable[[ChangeEvent], None]:
    """Print each event; for CSVs also the structural change (columns / delimiter / rows)."""
    def handle(ev: ChangeEvent) -> None:
        out = stream or sys.stdout
        line = f"[{ev.detected_at}] {ev}"
        if details and ev.kind in ("added", "changed"):
            try:
                ch = ev.csv_changes()
                if ch:
                    line += f"   ({ch.summary()})" + ("  ⚠ SCHEMA CHANGE" if ch.schema_changed else "")
            except Exception as e:  # unparsable CSV is itself worth seeing
                line += f"   (csv unreadable: {e})"
        print(line, file=out, flush=True)
    handle.__name__ = "console"
    return handle


def webhook(url: str, *, timeout: float = 10.0, retries: int = 3,
            headers: dict[str, str] | None = None) -> Callable[[ChangeEvent], None]:
    """POST each event as JSON to ``url`` (e.g. a Slack/Teams/Discord incoming webhook
    or your own backend). Includes a ``text`` field so chat webhooks render it."""
    client = httpx.Client(timeout=timeout, headers=headers)

    def handle(ev: ChangeEvent) -> None:
        payload = {"text": f"Soony data {ev.kind}: {ev.path}", "event": ev.to_dict()}
        for attempt in range(1, retries + 1):
            try:
                client.post(url, json=payload).raise_for_status()
                return
            except httpx.HTTPError as e:
                if attempt == retries:
                    raise
                log.warning("webhook attempt %d failed: %s", attempt, e)
    handle.__name__ = f"webhook({httpx.URL(url).host})"
    return handle


def run_command(cmd: str, *, timeout: float = 300.0) -> Callable[[ChangeEvent], None]:
    """Run a shell command per event. The event is passed as env vars:
    SOONY_EVENT_KIND, SOONY_EVENT_PATH, SOONY_EVENT_LOCAL, SOONY_EVENT_PREVIOUS,
    SOONY_EVENT_SHA256, SOONY_EVENT_SUPPLIER, SOONY_EVENT_JSON."""
    def handle(ev: ChangeEvent) -> None:
        env = {
            **os.environ,
            "SOONY_EVENT_KIND": ev.kind,
            "SOONY_EVENT_PATH": ev.path,
            "SOONY_EVENT_LOCAL": str(ev.local_path or ""),
            "SOONY_EVENT_PREVIOUS": str(ev.previous_copy or ""),
            "SOONY_EVENT_SHA256": ev.sha256 or "",
            "SOONY_EVENT_SUPPLIER": ev.supplier_id or "",
            "SOONY_EVENT_JSON": json.dumps(ev.to_dict(), default=str),
        }
        res = subprocess.run(cmd, shell=True, env=env, timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError(f"command exited {res.returncode}: {cmd}")
    handle.__name__ = f"exec({cmd[:30]})"
    return handle

"""The `gtl` command line."""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Any

from gtl import dispatch
from gtl.config import load_env, load_playbooks, load_tenant


class _SystemClock:
    def __init__(self, tz: str) -> None:
        from zoneinfo import ZoneInfo
        self._tz = ZoneInfo(tz)

    def now(self):  # type: ignore[no-untyped-def]
        from datetime import datetime
        return datetime.now(self._tz)


def _real(module: str, name: str, *args: Any) -> Any | None:
    try:
        return getattr(importlib.import_module(module), name)(*args)
    except (ImportError, AttributeError):
        return None


def build_context(tenant_id: str, db_path: str = "var/board.db"):  # type: ignore[no-untyped-def]
    from gtl import testing
    from gtl.board import SqliteBoard
    from gtl.contracts import Context, Ports
    from gtl.llm import LLMClient

    tenant = load_tenant(tenant_id)
    clock = _SystemClock(tenant.get("timezone", "Europe/Berlin"))
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    board = SqliteBoard(db_path, now=clock.now)
    files = _real("gtl.ingest", "PortalFiles", tenant)
    mail = _real("gtl.comms", "AgentMailbox", tenant)
    room = _real("gtl.api", "BoardControlRoom", board)
    for label, port in (("files", files), ("mail", mail), ("control room", room)):
        if port is None:
            print(f"warning: no real {label} port yet, using a fake", file=sys.stderr)
    ports = Ports(files=files or testing.FakeFiles(), mail=mail or testing.FakeMailbox(),
                  control_room=room or testing.FakeControlRoom(), clock=clock)
    llm = LLMClient(on_call=lambda payload: board.log_event("llm", "llm.call", payload))
    return Context(board=board, ports=ports, llm=llm, tenant=tenant, world="real",
                   playbooks=load_playbooks())


def cmd_run(args: argparse.Namespace) -> int:
    from gtl.govern.policy import seed_config_claims
    from gtl.registry import load_agents
    load_env()
    ctx = build_context(args.tenant, args.db)
    seed_config_claims(ctx)
    poll = args.poll_seconds or ctx.tenant.get("sources", {}).get("poll_seconds", 30)
    agents = load_agents()
    print(f"gtl run: tenant={args.tenant} agents={[a.name for a in agents]} poll={poll}s")
    try:
        dispatch.run(ctx, agents, poll)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        from gtl.api import create_app
    except ImportError as exc:
        print(f"gtl serve: the control room API cannot be imported ({exc}). "
              "Run `uv sync --extra dev` or check src/gtl/api.", file=sys.stderr)
        return 1
    import uvicorn
    from gtl.board import SqliteBoard
    load_env()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    uvicorn.run(create_app(SqliteBoard(args.db), load_tenant(args.tenant), live_mail=True), host=args.host, port=args.port)
    return 0


def cmd_gym(argv: list[str]) -> int:
    try:
        gym_cli = importlib.import_module("gtl.gym.cli")
    except ImportError:
        print("gtl gym: not built yet")
        return 0
    return int(gym_cli.main(argv) or 0)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["gym"]:
        return cmd_gym(argv[1:])  # the gym owns its own arguments, flags included
    parser = argparse.ArgumentParser(prog="gtl")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the dispatcher")
    run.add_argument("--tenant", default="soony")
    run.add_argument("--db", default="var/board.db")
    run.add_argument("--poll-seconds", type=float, default=0)
    run.set_defaults(fn=cmd_run)
    serve = sub.add_parser("serve", help="serve the control room API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--db", default="var/board.db")
    serve.add_argument("--tenant", default="soony")
    serve.set_defaults(fn=cmd_serve)
    sub.add_parser("gym", help="simulation and learning loop (arguments go to gtl.gym.cli)")
    args = parser.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())

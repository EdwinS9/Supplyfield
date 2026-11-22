"""Command line:  python -m soony_api <command>   (or `soony-api` once installed)

  ping                      check token + connectivity
  ls [--prefix P] [--json]  list portal files
  get PATH [-o OUT]         download one file (stdout if no -o)
  sync [DEST] [--prefix P] [--prune] [--dry-run]
  watch [DEST] [--interval S] [--pattern GLOB] [--kind K] [--exec CMD] [--webhook URL]
  status [DEST]             show the running listener's health
  archive [-o soony-data.zip]
  request METHOD PATH       raw call, prints JSON (for exploring new endpoints)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import SoonyClient, SoonyError
from . import handlers
from .listener import DataListener


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="soony-api", description="Soony Supplier Portal client")
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--base-url")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping")

    ls = sub.add_parser("ls")
    ls.add_argument("--prefix")
    ls.add_argument("--json", action="store_true")

    g = sub.add_parser("get")
    g.add_argument("path")
    g.add_argument("-o", "--out")

    for name in ("sync", "watch"):
        s = sub.add_parser(name)
        s.add_argument("dest", nargs="?")
        s.add_argument("--prefix")
        s.add_argument("--prune", action="store_true")
        s.add_argument("--no-history", action="store_true")
        s.add_argument("--baseline", type=Path, help="seed the change report from an existing portal mirror")
        s.add_argument("--workers", type=int, default=4)
        if name == "sync":
            s.add_argument("--dry-run", action="store_true")
            s.add_argument("--verify", action="store_true", help="re-hash all local files")
        else:
            s.add_argument("--interval", type=float, default=60)
            s.add_argument("--verify-every", type=int, default=10, help="full local re-hash every N polls")
            s.add_argument("--pattern", action="append", help="only dispatch paths matching glob (repeatable)")
            s.add_argument("--kind", action="append", choices=["added", "changed", "removed", "restored"])
            s.add_argument("--supplier", help="only events for this supplier id")
            s.add_argument("--exec", dest="exec_cmd", help="shell command per event (SOONY_EVENT_* env)")
            s.add_argument("--webhook", help="POST each event as JSON to this URL")
            s.add_argument("--emit-on-bootstrap", action="store_true")

    st = sub.add_parser("status")
    st.add_argument("dest", nargs="?")

    a = sub.add_parser("archive")
    a.add_argument("-o", "--out", default="soony-data.zip")

    r = sub.add_parser("request")
    r.add_argument("method")
    r.add_argument("path")
    r.add_argument("--data", help="JSON body")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING - 10 * min(args.verbose + (1 if args.cmd in ("sync", "watch") else 0), 2),
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.DEBUG if args.verbose >= 2 else logging.WARNING)
    try:
        with SoonyClient.from_env(base_url=args.base_url) as api:
            return _run(api, args)
    except SoonyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _run(api: SoonyClient, args: argparse.Namespace) -> int:
    if args.cmd == "ping":
        api.ping()
        print(f"ok – {api!r}")
    elif args.cmd == "ls":
        files = api.files.list(prefix=args.prefix)
        if args.json:
            print(json.dumps([f.to_dict() for f in files], indent=1))
        else:
            for f in files:
                print(f"{f.bytes:>10}  {f.sha256[:12]}  {f.path}")
            print(f"{len(files)} files", file=sys.stderr)
    elif args.cmd == "get":
        if args.out:
            print(api.files.download(args.path, args.out))
        else:
            sys.stdout.buffer.write(api.files.get(args.path))
    elif args.cmd == "sync":
        report = api.files.sync(args.dest, prefix=args.prefix, prune=args.prune,
                                keep_history=not args.no_history, workers=args.workers,
                                dry_run=args.dry_run, verify_local=args.verify,
                                human_report=True, baseline_dir=args.baseline)
        print(report.summary())
        for ev in report.events:
            print(f"  {ev}")
        for p, err in report.failed.items():
            print(f"  ! {p}: {err}", file=sys.stderr)
        return 2 if report.failed else 0
    elif args.cmd == "watch":
        listener = DataListener(
            api, args.dest, interval=args.interval, verify_every=args.verify_every,
            emit_on_bootstrap=args.emit_on_bootstrap, prefix=args.prefix, prune=args.prune,
            keep_history=not args.no_history, workers=args.workers, baseline_dir=args.baseline,
        )
        filt = dict(pattern=args.pattern, supplier=args.supplier)
        listener.subscribe(handlers.console(), args.kind, **filt)
        if args.exec_cmd:
            listener.subscribe(handlers.run_command(args.exec_cmd), args.kind, **filt)
        if args.webhook:
            listener.subscribe(handlers.webhook(args.webhook), args.kind, **filt)
        print(f"watching {listener.dest} every {args.interval:.0f}s – Ctrl-C to stop", file=sys.stderr)
        listener.run_forever()
    elif args.cmd == "status":
        from .listener import STATUS_FILE

        p = Path(args.dest or api.settings.cache_dir) / STATUS_FILE
        if not p.is_file():
            print(f"no listener status at {p}", file=sys.stderr)
            return 1
        print(p.read_text())
    elif args.cmd == "archive":
        print(api.files.archive(args.out))
    elif args.cmd == "request":
        body = json.loads(args.data) if args.data else None
        out = api.request(args.method, args.path, json=body)
        print(json.dumps(out, indent=1) if isinstance(out, (dict, list)) else out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

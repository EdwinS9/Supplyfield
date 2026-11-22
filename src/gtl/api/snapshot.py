"""A read-only snapshot of the control room for hosting: the Board, the file index and the Gym curves.

`take` copies what the screens read out of a working `var/` into a folder small enough to commit.
`hosted_app` serves it. The hosting file system is read-only, so at start the snapshot is copied to a
writable folder and the file archive (`raw/<sha256>/<path>`) is rebuilt there from the tenant's mirror;
the archive is never committed or uploaded twice. Anything written while serving is lost on restart.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

from fastapi import FastAPI

MANIFEST = "snapshot.json"
BOARD = "board.db"
INDEX = "portal-current.json"


def take(var: Path, out: Path, tenant_id: str, run: str | None = None,
         lead_post: str | list[str] | None = None) -> list[Path]:
    """Copy the Board and the files the screens read from `var` into `out`. Returns the files written.

    `run` is a one-line description of the run, shown with the data source on every screen.
    `lead_post` is the id of a notice on the Board that the screens lead with, or several: the first leads.
    """
    source = var / BOARD
    if not source.is_file():
        raise FileNotFoundError(f"No Board at {source}")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    written = [out / BOARD]
    # The backup API folds the write-ahead log into one self-contained file.
    with sqlite3.connect(source) as live, sqlite3.connect(out / BOARD) as copy:
        live.backup(copy)
        copy.execute("PRAGMA journal_mode=DELETE")
    patterns = [INDEX, "mail-translations.json", "mappings/*.json", "gym/*/curve.json", "gym/*/best/*.md"]
    for pattern in patterns:
        for path in sorted(var.glob(pattern)):
            if path.is_symlink() or not path.is_file():
                continue
            target = out / path.relative_to(var)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            written.append(target)
    manifest = out / MANIFEST
    meta = {"tenant": tenant_id, **({"run": run} if run else {}), **({"lead_post": lead_post} if lead_post else {})}
    manifest.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return [manifest, *written]


def restore_archive(snapshot: Path, data_root: Path) -> tuple[int, list[str]]:
    """Fill `snapshot/raw` with the current version of every indexed file found unchanged in `data_root`.

    Returns how many files were placed and the sources that were missing or differ from the index.
    """
    index = json.loads((snapshot / INDEX).read_text(encoding="utf-8")) if (snapshot / INDEX).is_file() else {}
    placed, skipped = 0, []
    root = data_root.resolve()
    for source, digest in sorted(index.items()):
        path = (root / source).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
            skipped.append(source)
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            skipped.append(source)
            continue
        target = snapshot / "raw" / digest / source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        placed += 1
    return placed, skipped


def hosted_app(repo: Path, snapshot: Path, work: Path, *, live_translate: bool = False) -> FastAPI:
    """The control room over a committed snapshot, with paths resolved from the repository root."""
    from gtl.api import create_app
    from gtl.board import SqliteBoard
    from gtl.config import load_tenant

    manifest = json.loads((snapshot / MANIFEST).read_text(encoding="utf-8"))
    # Tenant configs name their data folders relative to the repository root.
    os.chdir(repo)
    tenant = load_tenant(manifest["tenant"])
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(snapshot, work)
    _, skipped = restore_archive(work, Path(tenant["sources"]["data_root"]))
    if skipped:
        print(f"control room: {len(skipped)} indexed files are not in the mirror; their evidence returns 404")
    return create_app(SqliteBoard(str(work / BOARD)), tenant, var_dir=work, live_translate=live_translate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy the Board and the screen files out of var/ for hosting.")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--var", type=Path, default=Path("var"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--run", help="one line on the run, shown with the data source on every screen")
    parser.add_argument("--lead-post", nargs="+", help="ids of notices on the Board that the screens lead with; the first leads")
    args = parser.parse_args(argv)
    for path in take(args.var, args.out, args.tenant, args.run, args.lead_post):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

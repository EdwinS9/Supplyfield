"""Incremental mirror of the portal, keyed on SHA-256.

The portal changes during the hack without notice (new files, replaced files).
``sync_files`` downloads only what differs, keeps replaced versions under
``.history/`` so you can diff them, appends every change to
``.soony_changes.jsonl`` and returns typed ``ChangeEvent``s.

Layout under ``dest``:
    <remote path...>             current files, same tree as the portal
    .soony_manifest.json         {path: {sha256, bytes, synced_at}}
    .soony_changes.jsonl         one JSON line per change event (paths relative to dest)
    .soony.lock                  held while a sync runs (cross-process safety)
    .history/<path>.<sha8><ext>  previous versions of replaced/removed files
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator

from .errors import AuthenticationError, PermissionDeniedError, SoonyError
from .change_report import freeze_baseline, refresh_report, report_path
from .events import ChangeEvent, ChangeKind, now_iso
from .resources.files import DataFile, safe_local_path, sha256_file

if TYPE_CHECKING:
    from .resources.files import FilesResource

log = logging.getLogger("soony_api")

MANIFEST = ".soony_manifest.json"
CHANGELOG = ".soony_changes.jsonl"
LOCKFILE = ".soony.lock"
HISTORY = ".history"


class SyncLockedError(SoonyError):
    """Another process is syncing the same directory."""


@dataclass
class SyncReport:
    events: list[ChangeEvent] = field(default_factory=list)
    unchanged: int = 0
    remote_files: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    bootstrap: bool = False          # manifest did not exist before this run
    dry_run: bool = False
    seconds: float = 0.0

    def of(self, kind: ChangeKind) -> list[ChangeEvent]:
        return [e for e in self.events if e.kind == kind]

    @property
    def added(self) -> list[ChangeEvent]:
        return self.of("added")

    @property
    def changed(self) -> list[ChangeEvent]:
        return self.of("changed")

    @property
    def removed(self) -> list[ChangeEvent]:
        return self.of("removed")

    @property
    def restored(self) -> list[ChangeEvent]:
        return self.of("restored")

    @property
    def has_changes(self) -> bool:
        return bool(self.events)

    def summary(self) -> str:
        s = (f"+{len(self.added)} added, ~{len(self.changed)} changed, -{len(self.removed)} removed, "
             f"{self.unchanged} unchanged")
        if self.restored:
            s += f", {len(self.restored)} restored locally"
        if self.failed:
            s += f", {len(self.failed)} FAILED"
        return ("[dry run] " if self.dry_run else "") + s + f" ({self.seconds:.1f}s)"


# --------------------------------------------------------------------- helpers
def load_manifest(root: Path) -> dict[str, dict] | None:
    p = root / MANIFEST
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("manifest unreadable, rebuilding from disk hashes")
        return None


def _write_json_atomic(path: Path, data: object) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _archive_old(root: Path, rel: str, local: Path, old_sha: str) -> Path | None:
    if not local.is_file():
        return None
    target = safe_local_path(root / HISTORY, rel)
    target = target.with_name(f"{target.stem}.{old_sha[:8]}{target.suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local, target)
    return target


def log_entry(ev: ChangeEvent, root: Path) -> dict:
    """The change log line for ``ev``: local paths relative to the mirror, so the committed log names no
    home folder and stays valid on every machine."""
    d = ev.to_dict()
    for k in ("local_path", "previous_copy"):
        if d.get(k):
            p = Path(d[k])
            d[k] = p.relative_to(root).as_posix() if p.is_relative_to(root) else d[k]
    return d


@contextlib.contextmanager
def dir_lock(root: Path, timeout: float = 30.0) -> Iterator[None]:
    """Exclusive advisory lock on ``root/.soony.lock`` (no-op where fcntl is unavailable)."""
    try:
        import fcntl
    except ImportError:  # Windows: best effort, no lock
        yield
        return
    fh = (root / LOCKFILE).open("a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SyncLockedError(f"another sync holds {root / LOCKFILE}") from None
                time.sleep(0.2)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


# ------------------------------------------------------------------------ sync
def sync_files(
    files: "FilesResource",
    dest: Path,
    *,
    prefix: str | None = None,
    prune: bool = False,
    keep_history: bool = True,
    verify_local: bool = False,
    workers: int = 4,
    dry_run: bool = False,
    lock_timeout: float = 30.0,
    on_change: Callable[[ChangeEvent], None] | None = None,
    human_report: bool = False,
    baseline_dir: Path | None = None,
) -> SyncReport:
    """Bring ``dest`` in line with the portal.

    prefix:        only sync paths starting with this (e.g. ``"suppliers/<supplier-id>"``).
    prune:         delete local files that disappeared remotely (copied to .history first).
    verify_local:  re-hash every local file, re-downloading ones that were edited/deleted
                   locally (emitted as ``restored``). Without it, unchanged files are
                   trusted from the manifest (fast path) – missing files are always caught.
    dry_run:       report what would change; download nothing.
    on_change:     called synchronously with each ChangeEvent (prefer DataListener for
                   long-running handlers).
    """
    started = time.monotonic()
    root = dest.resolve()
    root.mkdir(parents=True, exist_ok=True)

    with dir_lock(root, lock_timeout):
        loaded = load_manifest(root)
        manifest: dict[str, dict] = loaded or {}
        report = SyncReport(bootstrap=loaded is None, dry_run=dry_run)

        remote = files.list(prefix=prefix)
        report.remote_files = len(remote)
        remote_by_path = {f.path: f for f in remote}

        if human_report and not dry_run:
            output = report_path(root)
            if not output.with_suffix(".baseline.json").exists():
                source = baseline_dir.resolve() if baseline_dir else root
                baseline = load_manifest(source) if source != root else loaded
                if source != root and baseline is None:
                    raise SoonyError(f"No readable baseline manifest in {source}")
                # A fresh mirror starts with the first listing, even if a download needs a retry.
                if baseline is None:
                    baseline = {f.path: {"sha256": f.sha256} for f in remote}
                elif prefix and source != root:
                    baseline = {p: entry for p, entry in baseline.items()
                                if p.startswith(prefix) or p in manifest}
                freeze_baseline(output, source, baseline)

        # kind: added | changed | restored
        todo: list[tuple[ChangeKind, DataFile]] = []
        for f in remote:
            local = safe_local_path(root, f.path)
            known = manifest.get(f.path)
            known_sha = known.get("sha256") if known else None
            exists = local.is_file()

            if known_sha == f.sha256 and exists and not verify_local:
                report.unchanged += 1
                continue
            disk_sha = sha256_file(local) if exists else None
            if disk_sha == f.sha256:
                if known_sha != f.sha256:
                    manifest[f.path] = {"sha256": f.sha256, "bytes": f.bytes, "synced_at": now_iso()}
                report.unchanged += 1
            elif known_sha == f.sha256:
                todo.append(("restored", f))      # portal same, local copy missing/edited
            elif known_sha is None and not exists:
                todo.append(("added", f))
            else:
                todo.append(("changed", f))       # portal has a new version

        removed = [p for p in manifest if p not in remote_by_path and (not prefix or p.startswith(prefix))]

        if dry_run:
            report.events = [
                ChangeEvent(kind=k, path=f.path, sha256=f.sha256, bytes=f.bytes,
                            previous_sha256=(manifest.get(f.path) or {}).get("sha256"))
                for k, f in todo
            ] + [ChangeEvent(kind="removed", path=p, previous_sha256=manifest[p].get("sha256")) for p in removed]
            report.seconds = time.monotonic() - started
            return report

        def fetch(kind: ChangeKind, f: DataFile) -> tuple[ChangeKind, DataFile, Path, str | None, Path | None]:
            local = safe_local_path(root, f.path)
            old_sha = (manifest.get(f.path) or {}).get("sha256")
            prev = None
            if kind == "changed" and local.is_file():
                # archive what we actually had on disk, labelled by its real hash
                disk_sha = sha256_file(local)
                old_sha = old_sha or disk_sha
                if keep_history:
                    prev = _archive_old(root, f.path, local, disk_sha)
            files.download(f.path, local, expected_sha256=f.sha256)
            return kind, f, local, old_sha, prev

        with (root / CHANGELOG).open("a", encoding="utf-8") as changelog:
            def emit(ev: ChangeEvent) -> None:
                report.events.append(ev)
                changelog.write(json.dumps(log_entry(ev, root), default=str) + "\n")
                changelog.flush()
                log.info("%s", ev)
                if on_change:
                    try:
                        on_change(ev)
                    except Exception:  # a buggy callback must not break the sync
                        log.exception("on_change callback failed for %s", ev.path)

            try:
                with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                    futures = {pool.submit(fetch, kind, f): f for kind, f in todo}
                    for fut in as_completed(futures):
                        f = futures[fut]
                        try:
                            kind, f, local, old_sha, prev = fut.result()
                        except (AuthenticationError, PermissionDeniedError):
                            for pending in futures:
                                pending.cancel()
                            raise
                        except (SoonyError, OSError) as e:
                            report.failed[f.path] = str(e)
                            log.error("sync failed for %s: %s", f.path, e)
                            continue
                        manifest[f.path] = {"sha256": f.sha256, "bytes": f.bytes, "synced_at": now_iso()}
                        emit(ChangeEvent(kind=kind, path=f.path, sha256=f.sha256, bytes=f.bytes,
                                         previous_sha256=old_sha if kind != "added" else None,
                                         local_path=local, previous_copy=prev))

                for p in removed:
                    entry = manifest.pop(p)
                    local = safe_local_path(root, p)
                    prev = None
                    if prune and local.is_file():
                        if keep_history:
                            prev = _archive_old(root, p, local, entry.get("sha256") or sha256_file(local))
                        local.unlink()
                    emit(ChangeEvent(kind="removed", path=p, previous_sha256=entry.get("sha256"),
                                     local_path=None if prune else local, previous_copy=prev))
            finally:
                _write_json_atomic(root / MANIFEST, manifest)
        if human_report and not report.failed:
            refresh_report(root, output, manifest)

    report.seconds = time.monotonic() - started
    return report

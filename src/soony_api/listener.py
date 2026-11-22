"""DataListener – watch the portal and react to every data change.

The portal has no webhooks / ETags (``Cache-Control: no-store``), so we poll the
file list (one cheap call carrying every SHA-256) and diff it via ``sync_files``.

    from soony_api import SoonyClient, DataListener

    api = SoonyClient.from_env()
    listener = DataListener(api, "soony-data", interval=60)

    @listener.on("changed", pattern="soony/erp/*.csv")
    def erp_changed(ev):
        ch = ev.csv_changes()
        print(ev.path, ch.summary() if ch else "")

    @listener.on(supplier="supplier-id")
    def karton_nowak(ev):
        print("Karton Nowak:", ev)

    listener.run_forever()          # blocking; Ctrl-C / SIGTERM stop cleanly
    # or: listener.start() ... listener.stop()   (background thread)

Robustness:
  * handlers run on a separate dispatcher thread – a slow/crashing handler never
    stalls polling, and one handler's exception doesn't affect the others
  * transient failures back off exponentially (capped), then resume the normal cadence
  * a bad token (401/403) stops the listener instead of hammering the portal
  * cross-process lock on the cache dir, atomic writes, SHA-256-verified downloads
  * periodic full local verification catches files edited/deleted on disk
  * health is written to ``<dest>/.soony_status.json`` for dashboards / teammates
"""

from __future__ import annotations

import fnmatch
import logging
import queue
import random
import signal
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from .errors import AuthenticationError, PermissionDeniedError, SoonyError
from .events import ChangeEvent, ChangeKind, now_iso
from .sync import SyncReport, _write_json_atomic, sync_files

if TYPE_CHECKING:
    from .client import SoonyClient

log = logging.getLogger("soony_api.listener")

STATUS_FILE = ".soony_status.json"
Handler = Callable[[ChangeEvent], None]
ReportHandler = Callable[[SyncReport], None]
_STOP = object()


@dataclass
class Subscription:
    handler: Handler
    kinds: frozenset[str] | None = None        # None = all kinds
    patterns: tuple[str, ...] = ()             # fnmatch on remote path; empty = all
    supplier: str | None = None
    name: str = ""
    calls: int = 0
    errors: int = 0

    def matches(self, ev: ChangeEvent) -> bool:
        if self.kinds is not None and ev.kind not in self.kinds:
            return False
        if self.supplier and ev.supplier_id != self.supplier:
            return False
        if self.patterns and not any(fnmatch.fnmatchcase(ev.path, p) for p in self.patterns):
            return False
        return True


@dataclass
class ListenerStatus:
    state: str = "idle"                 # idle | running | backoff | stopped | failed
    dest: str = ""
    interval: float = 0.0
    started_at: str | None = None
    last_poll_at: str | None = None
    last_success_at: str | None = None
    last_change_at: str | None = None
    next_poll_at: str | None = None
    polls: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    remote_files: int = 0
    events_total: int = 0
    events_by_kind: dict[str, int] = field(default_factory=dict)
    handler_errors: int = 0
    pending_events: int = 0


class DataListener:
    def __init__(
        self,
        client: "SoonyClient",
        dest: str | Path | None = None,
        *,
        interval: float = 60.0,
        jitter: float = 0.1,
        max_backoff: float = 600.0,
        verify_every: int = 10,
        emit_on_bootstrap: bool = False,
        prefix: str | None = None,
        prune: bool = False,
        keep_history: bool = True,
        workers: int = 4,
        baseline_dir: Path | None = None,
        max_consecutive_failures: int | None = None,
    ) -> None:
        """
        interval:          seconds between polls
        jitter:            ±fraction randomisation of the interval (avoid lock-step teams)
        max_backoff:       cap for exponential backoff after failures
        verify_every:      every N polls re-hash all local files (0 = never)
        emit_on_bootstrap: if the cache has no manifest yet, the first sync would report
                           every file as 'added'; by default those are logged but not
                           dispatched to handlers
        max_consecutive_failures: give up after this many failures in a row (None = never)
        """
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self.client = client
        self.dest = Path(dest or client.settings.cache_dir).resolve()
        self.interval = interval
        self.jitter = jitter
        self.max_backoff = max_backoff
        self.verify_every = verify_every
        self.emit_on_bootstrap = emit_on_bootstrap
        self.max_consecutive_failures = max_consecutive_failures
        self.sync_kwargs = dict(prefix=prefix, prune=prune, keep_history=keep_history, workers=workers,
                                human_report=True, baseline_dir=baseline_dir)

        self._subs: list[Subscription] = []
        self._report_handlers: list[ReportHandler] = []
        self._subs_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._events: "queue.Queue[object]" = queue.Queue()
        self._poll_thread: threading.Thread | None = None
        self._dispatch_thread: threading.Thread | None = None
        self.status = ListenerStatus(dest=str(self.dest), interval=interval)
        self.fatal_error: BaseException | None = None

    # ------------------------------------------------------------ subscribe
    def subscribe(
        self,
        handler: Handler,
        kinds: ChangeKind | Iterable[ChangeKind] | None = None,
        *,
        pattern: str | Iterable[str] | None = None,
        supplier: str | None = None,
        name: str | None = None,
    ) -> Subscription:
        if isinstance(kinds, str):
            kinds = None if kinds in ("*", "all") else [kinds]
        if isinstance(pattern, str):
            pattern = [pattern]
        sub = Subscription(
            handler=handler,
            kinds=frozenset(kinds) if kinds is not None else None,
            patterns=tuple(pattern or ()),
            supplier=supplier,
            name=name or getattr(handler, "__name__", repr(handler)),
        )
        with self._subs_lock:
            self._subs.append(sub)
        return sub

    def on(self, kinds: ChangeKind | Iterable[ChangeKind] | None = None, **kw: object) -> Callable[[Handler], Handler]:
        """Decorator form of ``subscribe``."""
        def deco(fn: Handler) -> Handler:
            self.subscribe(fn, kinds, **kw)  # type: ignore[arg-type]
            return fn
        return deco

    def unsubscribe(self, sub: Subscription) -> None:
        with self._subs_lock:
            self._subs = [s for s in self._subs if s is not sub]

    def on_report(self, fn: ReportHandler) -> ReportHandler:
        """Called (on the dispatcher thread) after every successful poll, changes or not."""
        self._report_handlers.append(fn)
        return fn

    # ---------------------------------------------------------------- polling
    def poll_once(self, *, verify_local: bool = False) -> SyncReport:
        """One sync cycle; queues events for dispatch. Raises on failure."""
        self.status.last_poll_at = now_iso()
        try:
            report = sync_files(self.client.files, self.dest, verify_local=verify_local, **self.sync_kwargs)
        finally:
            self.status.polls += 1          # counts completed attempts (success or failure)

        st = self.status
        if not report.failed:
            st.last_success_at = now_iso()
            st.consecutive_failures = 0
            st.last_error = None
        st.remote_files = report.remote_files
        if report.events:
            st.last_change_at = now_iso()
            st.events_total += len(report.events)
            for ev in report.events:
                st.events_by_kind[ev.kind] = st.events_by_kind.get(ev.kind, 0) + 1

        if report.bootstrap and not self.emit_on_bootstrap and report.events:
            log.info("bootstrap sync: %s (not dispatched; emit_on_bootstrap=False)", report.summary())
        else:
            for ev in report.events:
                self._events.put(ev)
        if report.has_changes or report.failed:
            log.info("poll: %s", report.summary())
        else:
            log.debug("poll: %s", report.summary())
        if not report.failed:
            self._events.put(report)
        return report

    def trigger(self) -> None:
        """Poll now instead of waiting for the next interval."""
        self._wake.set()

    def _next_delay(self) -> float:
        f = self.status.consecutive_failures
        # healthy: normal cadence; failing: 5s, 10s, 20s, ... capped at max_backoff
        base = self.interval if f == 0 else min(self.max_backoff, 5.0 * 2 ** min(f - 1, 10))
        return max(min(1.0, self.interval), base * (1 + random.uniform(-self.jitter, self.jitter)))

    def _poll_loop(self) -> None:
        st = self.status
        st.state, st.started_at = "running", now_iso()
        verify = False
        try:
            while not self._stop.is_set():
                if self.verify_every > 0 and (st.polls + 1) % self.verify_every == 0:
                    verify = True
                try:
                    report = self.poll_once(verify_local=verify)
                    if report.failed:
                        raise SoonyError(f"Incomplete sync: {report.summary()}")
                    verify = False
                    st.state = "running"
                except (AuthenticationError, PermissionDeniedError) as e:
                    st.consecutive_failures += 1
                    st.state, st.last_error = "failed", str(e)
                    log.error("listener stopping – token rejected: %s", e)
                    self.fatal_error = e
                    break
                except Exception as e:  # network, server, disk, ... -> back off and retry
                    st.consecutive_failures += 1
                    st.state, st.last_error = "backoff", f"{type(e).__name__}: {e}"
                    log.warning("poll failed (%d in a row): %s", st.consecutive_failures, st.last_error)
                    if self.max_consecutive_failures and st.consecutive_failures >= self.max_consecutive_failures:
                        st.state = "failed"
                        self.fatal_error = e
                        log.error("listener giving up after %d consecutive failures", st.consecutive_failures)
                        break

                delay = self._next_delay()
                st.next_poll_at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() + delay))
                self._write_status()
                # wait for interval, an explicit trigger(), or stop()
                self._wake.wait(delay)
                self._wake.clear()
        finally:
            if st.state != "failed":
                st.state = "stopped"
            st.next_poll_at = None
            self._write_status()
            self._events.put(_STOP)

    # -------------------------------------------------------------- dispatch
    def _dispatch_loop(self) -> None:
        while True:
            item = self._events.get()
            if item is _STOP:
                return
            self.status.pending_events = self._events.qsize()
            if isinstance(item, SyncReport):
                for fn in list(self._report_handlers):
                    self._safe_call(fn, item, getattr(fn, "__name__", "report_handler"))
                continue
            ev: ChangeEvent = item  # type: ignore[assignment]
            with self._subs_lock:
                subs = [s for s in self._subs if s.matches(ev)]
            for sub in subs:
                sub.calls += 1
                if not self._safe_call(sub.handler, ev, sub.name):
                    sub.errors += 1

    def _safe_call(self, fn: Callable, arg: object, name: str) -> bool:
        try:
            fn(arg)
            return True
        except Exception:
            self.status.handler_errors += 1
            log.exception("handler %r failed on %s", name, arg if isinstance(arg, ChangeEvent) else "report")
            return False

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "DataListener":
        """Run in background threads (returns immediately)."""
        if self._poll_thread and self._poll_thread.is_alive():
            return self
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            raise RuntimeError("Previous listener dispatcher is still draining")
        self.fatal_error = None
        self._stop.clear()
        self._wake.clear()
        self._dispatch_thread = threading.Thread(target=self._dispatch_loop, name="soony-dispatch", daemon=True)
        self._poll_thread = threading.Thread(target=self._poll_loop, name="soony-poll", daemon=True)
        self._dispatch_thread.start()
        self._poll_thread.start()
        log.info("listening on %s every %.0fs (%d handlers)", self.dest, self.interval, len(self._subs))
        return self

    def stop(self, timeout: float | None = 30.0) -> None:
        """Stop polling, then drain already-queued events through the handlers."""
        self._stop.set()
        self._wake.set()
        if self._poll_thread:
            self._poll_thread.join(timeout)
        if self._dispatch_thread:
            self._dispatch_thread.join(timeout)

    def join(self, timeout: float | None = None) -> None:
        if self._poll_thread:
            self._poll_thread.join(timeout)
        if self._dispatch_thread:
            self._dispatch_thread.join(timeout)

    @property
    def running(self) -> bool:
        return bool(self._poll_thread and self._poll_thread.is_alive())

    def run_forever(self) -> None:
        """Blocking run. SIGINT/SIGTERM trigger a clean stop. Re-raises a fatal error."""
        previous: dict[int, object] = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: self._request_stop())
        try:
            self.start()
            while self.running:
                self._poll_thread.join(0.5)  # type: ignore[union-attr]
        finally:
            self.stop()
            for sig, h in previous.items():
                signal.signal(sig, h)  # type: ignore[arg-type]
        if self.fatal_error:
            raise self.fatal_error

    def _request_stop(self) -> None:
        log.info("stop requested")
        self._stop.set()
        self._wake.set()

    def __enter__(self) -> "DataListener":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _write_status(self) -> None:
        self.status.pending_events = self._events.qsize()
        try:
            self.dest.mkdir(parents=True, exist_ok=True)
            data = asdict(self.status)
            data["handlers"] = [{"name": s.name, "calls": s.calls, "errors": s.errors} for s in self._subs]
            _write_json_atomic(self.dest / STATUS_FILE, data)
        except OSError as e:
            log.warning("could not write status file: %s", e)

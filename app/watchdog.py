"""Crash watchdog: automatic full restore on process exit.

Per CLAUDE.md's core design principle #3 ("Gig-safe defaults"): any
failure mode must resolve to "mics on their original patch, console
behaving stock" within ~1 second. This is the automatic half of that --
the web UI's manual Restore button calls
app.osc.routing_apply.restore_snapshot directly; this module makes the
same call happen on its own when the process is about to go away
unexpectedly.

Honest scope limit: this only guards *this process's own* exit paths --
an unhandled exception, SIGINT/SIGTERM, or normal interpreter shutdown all
run the restore before the process actually disappears. It cannot catch a
hard kill (SIGKILL), a segfault, or a power loss, since nothing in a
process that's already gone gets to run. A fully bulletproof version of
this would need a separate supervisor process pinging a heartbeat and
restoring independently of this one's survival -- that's out of scope
here and flagged rather than silently assumed away.
"""
from __future__ import annotations

import atexit
import signal
import sys
import threading
import time
from collections.abc import Callable

from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import OscConnection
from app.osc.routing_apply import restore_snapshot
from app.osc.routing_snapshot import RoutingSnapshot
from app.state import AppState

RestoreFn = Callable[..., list[str]]

_CAUGHT_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class Watchdog:
    def __init__(
        self,
        osc: OscConnection,
        diagnostics: DiagnosticsLogger,
        state: AppState,
        snapshot_provider: Callable[[], RoutingSnapshot | None],
        restore_fn: RestoreFn = restore_snapshot,
    ) -> None:
        self.osc = osc
        self.diagnostics = diagnostics
        self.state = state
        self.snapshot_provider = snapshot_provider
        self.restore_fn = restore_fn

        self._started = False
        self._restore_lock = threading.Lock()
        self._restored = False
        self._previous_excepthook: Callable | None = None
        self._previous_handlers: dict[int, object] = {}

    def start(self) -> None:
        """Installs signal handlers, an atexit hook, and an exception hook
        that all trigger a full restore before this process exits.
        Idempotent -- calling twice is a no-op."""
        if self._started:
            return
        self._started = True
        self._restored = False
        for sig in _CAUGHT_SIGNALS:
            self._previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, self._handle_signal)
        atexit.register(self._handle_atexit)
        self._previous_excepthook = sys.excepthook
        sys.excepthook = self._handle_exception

    def stop(self) -> None:
        """Restores the previous signal/exception handlers. Call this
        after a clean, user-initiated shutdown that already restored the
        snapshot itself, so that path isn't double-counted as a crash."""
        if not self._started:
            return
        self._started = False
        for sig, previous in self._previous_handlers.items():
            signal.signal(sig, previous)
        self._previous_handlers.clear()
        atexit.unregister(self._handle_atexit)
        if self._previous_excepthook is not None:
            sys.excepthook = self._previous_excepthook
            self._previous_excepthook = None

    def trigger_full_restore(self, reason: str) -> list[str]:
        """Replays the most recent routing snapshot. Idempotent per armed
        session -- a signal handler followed by the atexit hook it also
        triggers (the normal SIGTERM sequence) must not replay the
        snapshot twice. Returns the mismatch list from restore_snapshot
        (empty = clean restore)."""
        with self._restore_lock:
            if self._restored:
                return []
            self._restored = True

        snapshot = self.snapshot_provider()
        if snapshot is None:
            self.diagnostics.log_watchdog("restore_skipped_no_snapshot", {"reason": reason})
            return []

        started_at = time.monotonic()
        correlation_id = self.diagnostics.new_correlation_id()
        self.diagnostics.log_watchdog("crash_restore_triggered", {"reason": reason}, correlation_id=correlation_id)
        mismatches = self.restore_fn(
            self.osc, self.diagnostics, snapshot, self.state, correlation_id=correlation_id
        )
        self.diagnostics.log_watchdog(
            "crash_restore_completed",
            {
                "reason": reason,
                "elapsed_sec": time.monotonic() - started_at,
                "mismatches": mismatches,
            },
            correlation_id=correlation_id,
        )
        return mismatches

    # -- hook targets -----------------------------------------------------
    def _handle_signal(self, signum: int, frame) -> None:
        self.trigger_full_restore(reason=f"signal:{signal.Signals(signum).name}")
        # Re-raise through the default handler so the process still exits
        # the way it normally would for this signal (correct exit code,
        # no lingering process) once the restore is done.
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    def _handle_atexit(self) -> None:
        self.trigger_full_restore(reason="atexit")

    def _handle_exception(self, exc_type: type, exc_value: BaseException, exc_tb) -> None:
        self.diagnostics.log_error(exc_value, context="unhandled_exception")
        self.trigger_full_restore(reason=f"unhandled_exception:{exc_type.__name__}")
        if self._previous_excepthook is not None:
            self._previous_excepthook(exc_type, exc_value, exc_tb)

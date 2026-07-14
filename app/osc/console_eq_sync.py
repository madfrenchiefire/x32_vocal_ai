"""Internal-EQ mode: mirror the app's detected feedback notches into the
channel's own console 4-band EQ, continuously.

Per-channel `ChannelState.eq_mode`:

- **external** (default): the app processes the audio itself through its
  notch bank via the channel insert (app.osc.routing_apply). The console EQ
  is never touched.
- **internal**: the app stays OUT of the audio path -- no insert -- and
  instead writes the feedback notches its detector finds into the channel's
  console EQ (`/ch/NN/eq/...`). The app is only *listening* (it already
  reads every channel off the card for metering/analysis); the actual
  suppression happens in the desk's own EQ.

The detection pipeline is identical to external mode -- the app's
NotchFilterBank still accumulates the feedback frequencies (its dedup /
aging logic is what makes the notch set stable). This service just mirrors
that bank's current notches onto the console EQ whenever they change.

Gig-safety / not clobbering the engineer's EQ:

- The channel's full console EQ is snapshotted ONCE, the first time the app
  writes to it, into `AppState.console_eq_snapshots` -- restorable verbatim.
- When feedback subsides and the bank empties, the original EQ is restored.
- Switching the channel back to external mode (or `restore_all` on
  shutdown) restores it too.
- The X32 channel EQ only has 4 bands, so at most 4 feedback notches fit and
  they share those bands with any tonal EQ the engineer set -- that's the
  inherent trade-off of internal mode, which is why external (insert) is the
  default. Restore always puts the original bands back.

Like app.osc.gain_assist, the audio analysis thread must never block on OSC,
so :meth:`request_sync` only enqueues; a worker thread does the console I/O.
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from app.diagnostics.logger import DiagnosticsLogger
from app.osc.channel_eq import (
    ChannelEqError,
    restore_console_eq,
    snapshot_console_eq,
    write_notches_to_console_eq,
)
from app.osc.connection import OscConnection
from app.state import AppState

NotchesProvider = Callable[[int], list[dict]]


def _signature(notches: list[dict]) -> tuple:
    """A hashable summary of a channel's notch set, so the worker only
    writes the console EQ when it actually changed."""
    return tuple(sorted((round(n["frequency_hz"], 1), round(n["depth_db"], 2), round(n["q"], 3)) for n in notches))


class ConsoleEqSync:
    def __init__(
        self,
        osc: OscConnection | None,
        diagnostics: DiagnosticsLogger,
        state: AppState,
        notches_provider: NotchesProvider,
    ) -> None:
        self.osc = osc  # rewired live by /api/console/connect, like MidiService
        self.diagnostics = diagnostics
        self.state = state
        self.notches_provider = notches_provider

        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_written: dict[int, tuple] = {}  # channel -> last-synced notch signature

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker, name="console-eq-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- entry point (called from the audio analysis thread) ------------------

    def request_sync(self, channel: int) -> None:
        """Cheap and non-blocking -- safe from the analysis thread. The
        worker checks eq_mode and change-detects before touching the desk."""
        if self.osc is None:
            return
        self._queue.put(channel)

    # -- status / restore ------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            managed = sorted(
                ch for ch, c in self.state.channels.items() if c.eq_mode == "internal"
            )
            return {"internal_channels": managed, "synced_channels": sorted(self._last_written)}

    def restore_channel(self, channel: int, correlation_id: str | None = None) -> bool:
        """Put a channel's console EQ back to its pre-app snapshot and forget
        it (used when a channel leaves internal mode). Returns True if a
        snapshot existed to restore."""
        snapshot = self.state.console_eq_snapshots.get(channel)
        with self._lock:
            self._last_written.pop(channel, None)
        if snapshot is None or self.osc is None:
            return False
        restore_console_eq(self.osc, self.diagnostics, channel, snapshot, correlation_id=correlation_id)
        return True

    def restore_all(self, correlation_id: str | None = None) -> list[int]:
        with self._lock:
            channels = sorted(self._last_written)
        restored = [ch for ch in channels if self.restore_channel(ch, correlation_id=correlation_id)]
        return restored

    # -- worker ----------------------------------------------------------------

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                channel = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._sync_channel(channel)
            except Exception as exc:  # never let the worker die silently
                self.diagnostics.log_error(exc, context=f"console EQ sync for channel {channel} failed")

    def _sync_channel(self, channel: int) -> None:
        if self.osc is None:
            return
        channel_state = self.state.channels.get(channel)
        if channel_state is None or channel_state.eq_mode != "internal":
            return

        notches = self.notches_provider(channel)
        signature = _signature(notches)
        with self._lock:
            if self._last_written.get(channel) == signature:
                return  # nothing changed since the last sync

        correlation_id = self.diagnostics.new_correlation_id()
        # Snapshot the channel's console EQ once, before the first write, so
        # the engineer's original EQ is always restorable.
        if channel not in self.state.console_eq_snapshots:
            self.state.set_console_eq_snapshot(
                channel, snapshot_console_eq(self.osc, channel, correlation_id=correlation_id)
            )

        if not notches:
            # Feedback gone: put the channel's original EQ back.
            self.restore_channel(channel, correlation_id=correlation_id)
            self.diagnostics.log_state_change(
                "internal_eq_cleared", after={"channel": channel}, correlation_id=correlation_id
            )
            return

        try:
            written = write_notches_to_console_eq(
                self.osc, self.diagnostics, channel, notches, correlation_id=correlation_id
            )
        except ChannelEqError as exc:
            self.diagnostics.log_error(exc, context=f"internal EQ write for channel {channel}", correlation_id=correlation_id)
            return

        with self._lock:
            self._last_written[channel] = signature
        self.diagnostics.log_state_change(
            "internal_eq_synced",
            after={"channel": channel, "written": written},
            correlation_id=correlation_id,
        )

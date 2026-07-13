"""Opt-in last-resort preamp gain assist.

When a channel's notch bank is saturated (every allowed notch busy) and
the detector STILL finds new feedback candidates, notching has lost the
gain-before-feedback battle -- the only remaining lever is actual gain.
This module steps the offending channel's preamp (`/headamp/NNN/gain`,
linf -12..+60 dB in 0.5 dB steps, doc-confirmed) down by a small
configured amount, rate-limited and hard-capped.

Deliberately conservative, because touching gain changes the engineer's
mix:

- **Opt-in** (`AppConfig.gain_assist_enabled`, default False).
- Per-channel cooldown between trims; per-headamp session cap
  (`gain_assist_max_total_db`).
- Every trim is logged as a `watchdog` event -- loud, not buried.
- The first trim of each headamp snapshots its original gain;
  `restore_all()` (web UI button) puts everything back. NOT part of the
  crash watchdog's automatic restore: mid-crash, leaving a mic a few dB
  quieter is safer than restoring gain a runaway squeal forced down.

Which headamp feeds a channel: `/-ha/<ch-1>/index` (doc-confirmed,
read-only) -- but once the app has inserted a channel, its live source is
the Card return and `/-ha` can report -1, so the fallback derives the
pre-app source from the routing snapshot (its IN-block value + position
-> userrout-equivalent value -> headamp index: local 1-32 -> 0-31,
AES50-A 1-48 -> 32-79, AES50-B 1-48 -> 80-127).

The audio analysis thread must never block on OSC, so
:meth:`request_trim` only enqueues; a worker thread does the console I/O.
"""
from __future__ import annotations

import queue
import threading
import time

from app.config import AppConfig
from app.diagnostics.logger import DiagnosticsLogger
from app.osc import addresses
from app.osc.connection import OscConnection
from app.state import AppState

GAIN_MIN_DB, GAIN_MAX_DB = -12.0, 60.0
GAIN_STEP_DB = 0.5
GAIN_NORM_TOLERANCE = 1.5 * GAIN_STEP_DB / (GAIN_MAX_DB - GAIN_MIN_DB)


def headamp_gain_addr(headamp_index: int) -> str:
    if not 0 <= headamp_index <= 127:
        raise ValueError(f"headamp index must be 0-127, got {headamp_index}")
    return f"/headamp/{headamp_index:03d}/gain"


def ha_index_addr(channel: int) -> str:
    if not 1 <= channel <= 32:
        raise ValueError(f"channel must be 1-32, got {channel}")
    return f"/-ha/{channel - 1:02d}/index"


def gain_db_to_norm(db: float) -> float:
    db = min(max(db, GAIN_MIN_DB), GAIN_MAX_DB)
    return (db - GAIN_MIN_DB) / (GAIN_MAX_DB - GAIN_MIN_DB)


def gain_norm_to_db(norm: float) -> float:
    return GAIN_MIN_DB + (GAIN_MAX_DB - GAIN_MIN_DB) * min(max(norm, 0.0), 1.0)


def headamp_for_userrout_value(value: int | None) -> int | None:
    """Pre-app channel source (as a userrout-equivalent value) -> headamp
    index, or None for sources without a preamp (Card, Aux, OFF...)."""
    if value is None:
        return None
    if 1 <= value <= 32:  # Local Analog
        return value - 1
    if 33 <= value <= 80:  # AES50-A
        return 32 + (value - 33)
    if 81 <= value <= 128:  # AES50-B
        return 80 + (value - 81)
    return None


class GainAssist:
    def __init__(
        self,
        osc: OscConnection | None,
        diagnostics: DiagnosticsLogger,
        config: AppConfig,
        state: AppState,
    ) -> None:
        self.osc = osc  # rewired live by /api/console/connect, like MidiService
        self.diagnostics = diagnostics
        self.config = config
        self.state = state

        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._original_gains: dict[int, float] = {}  # headamp -> normalized gain before first trim
        self._applied_db: dict[int, float] = {}  # headamp -> total dB trimmed this session
        self._last_trim_monotonic: dict[int, float] = {}  # channel -> last trim time

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker, name="gain-assist", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- entry point (called from the audio analysis thread) ------------------

    def request_trim(self, channel: int) -> None:
        """Cheap and non-blocking -- safe to call from the analysis thread.
        The worker applies config/cooldown/cap checks."""
        if not self.config.gain_assist_enabled or self.osc is None:
            return
        self._queue.put(channel)

    # -- status / restore ------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self.config.gain_assist_enabled,
                "trims_db": {str(ha): db for ha, db in self._applied_db.items() if db > 0},
            }

    def restore_all(self, correlation_id: str | None = None) -> list[int]:
        """Write every trimmed headamp back to its pre-trim gain. Returns
        the headamp indices restored."""
        correlation_id = correlation_id or self.diagnostics.new_correlation_id()
        with self._lock:
            to_restore = dict(self._original_gains)
        restored = []
        for headamp, original_norm in to_restore.items():
            if self.osc is None:
                break
            self.osc.send(headamp_gain_addr(headamp), original_norm, correlation_id=correlation_id)
            restored.append(headamp)
            time.sleep(0.02)
        with self._lock:
            for headamp in restored:
                self._original_gains.pop(headamp, None)
                self._applied_db.pop(headamp, None)
        self.diagnostics.log_watchdog(
            "gain_assist_restored", {"headamps": restored}, correlation_id=correlation_id
        )
        return restored

    # -- worker ----------------------------------------------------------------

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                channel = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._maybe_trim(channel)
            except Exception as exc:  # never let the worker die silently
                self.diagnostics.log_error(exc, context=f"gain assist trim for channel {channel} failed")

    def _maybe_trim(self, channel: int) -> None:
        if not self.config.gain_assist_enabled or self.osc is None:
            return
        now = time.monotonic()
        last = self._last_trim_monotonic.get(channel)
        if last is not None and now - last < self.config.gain_assist_cooldown_sec:
            return
        self._last_trim_monotonic[channel] = now

        headamp = self._resolve_headamp(channel)
        if headamp is None:
            self.diagnostics.log_watchdog(
                "gain_assist_no_headamp",
                {"channel": channel, "detail": "channel's source has no resolvable preamp -- not trimming"},
            )
            return

        with self._lock:
            applied = self._applied_db.get(headamp, 0.0)
        if applied + self.config.gain_assist_step_db > self.config.gain_assist_max_total_db:
            self.diagnostics.log_watchdog(
                "gain_assist_cap_reached",
                {"channel": channel, "headamp": headamp, "applied_db": applied,
                 "cap_db": self.config.gain_assist_max_total_db},
            )
            return

        addr = headamp_gain_addr(headamp)
        try:
            (current_norm,) = self.osc.query(addr)
        except TimeoutError:
            self.diagnostics.log_watchdog(
                "gain_assist_read_failed", {"channel": channel, "headamp": headamp}
            )
            return

        with self._lock:
            self._original_gains.setdefault(headamp, float(current_norm))

        new_db = gain_norm_to_db(float(current_norm)) - self.config.gain_assist_step_db
        new_norm = gain_db_to_norm(new_db)
        self.osc.send(addr, new_norm)
        try:
            (readback,) = self.osc.query(addr)
        except TimeoutError:
            readback = None
        confirmed = isinstance(readback, (int, float)) and abs(float(readback) - new_norm) <= GAIN_NORM_TOLERANCE
        if confirmed:
            with self._lock:
                self._applied_db[headamp] = self._applied_db.get(headamp, 0.0) + self.config.gain_assist_step_db

        # Loud by design: every trim is a watchdog event, not a debug line.
        self.diagnostics.log_watchdog(
            "gain_assist_trimmed",
            {
                "channel": channel,
                "headamp": headamp,
                "step_db": self.config.gain_assist_step_db,
                "new_gain_db": round(new_db, 1),
                "total_trimmed_db": self._applied_db.get(headamp, 0.0),
                "confirmed": confirmed,
            },
        )

    def _resolve_headamp(self, channel: int) -> int | None:
        # Live mapping first...
        try:
            (index,) = self.osc.query(ha_index_addr(channel))
            if isinstance(index, int) and index >= 0:
                return index
        except TimeoutError:
            pass
        # ...then the pre-app source from the routing snapshot (-1/-timeout
        # is expected once the app has inserted the channel, since its live
        # source is then the Card return, not a preamp).
        snapshot = self.state.current_snapshot
        if snapshot is None:
            return None
        block_values = snapshot.routing.get("in", [])
        block_index = (channel - 1) // 8
        if block_index >= len(block_values) or block_values[block_index] is None:
            return None
        equivalent = addresses.equivalent_userrout_in_value(block_values[block_index], channel)
        return headamp_for_userrout_value(equivalent)

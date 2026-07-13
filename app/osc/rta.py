"""Console RTA streaming: the desk's own 100-band analyzer, in the web UI.

The console computes a 100-band RTA of whatever `/-stat/rtasource` points
at and streams it as `/meters/15` blobs (format in
app.osc.meters.decode_rta_blob). This module keeps that stream alive
(meter subscriptions time out after ~10 s, so the subscribe is re-sent
every renew_sec like `/xremote`) and hands each decoded 100-bin dB frame
to a callback -- app.web.sockets pushes them to the browser as `rta`
WebSocket events.

Pointing the RTA at a specific channel changes what the engineer sees on
the desk's own RTA overlay too, so per "snapshot before touching
anything" the previous `/-stat/rtasource` value is read first and written
back when streaming stops.
"""
from __future__ import annotations

import queue
import threading
import time

from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import OscConnection
from app.osc.meters import MeterBlobError, decode_rta_blob

RTA_METER_PATH = "/meters/15"
METERS_SUBSCRIBE_ADDRESS = "/meters"
RTA_SOURCE_ADDRESS = "/-stat/rtasource"
RENEW_SEC = 8.0  # meter subscriptions run out after ~10 s

# /-stat/rtasource values (doc-confirmed): 0-31 = channels 1-32 PRE-EQ.
RTA_SOURCE_CHANNEL_PRE_EQ_BASE = 0


def rta_source_for_channel(channel: int) -> int:
    if not 1 <= channel <= 32:
        raise ValueError(f"channel must be 1-32, got {channel}")
    return RTA_SOURCE_CHANNEL_PRE_EQ_BASE + channel - 1


class RtaStreamer:
    """Background thread keeping the /meters/15 subscription alive and
    decoding each blob to the on_rta callback. One instance per streaming
    session; start() once, stop() once."""

    def __init__(
        self,
        osc: OscConnection,
        diagnostics: DiagnosticsLogger,
        on_rta,
        renew_sec: float = RENEW_SEC,
    ) -> None:
        self.osc = osc
        self.diagnostics = diagnostics
        self.on_rta = on_rta
        self.renew_sec = renew_sec

        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._original_source: int | None = None
        self._source_changed = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, source: int | None = None, correlation_id: str | None = None) -> None:
        """Begin streaming. source, if given, is written to
        /-stat/rtasource (e.g. rta_source_for_channel(n)) after
        snapshotting the current value for restore-on-stop; None leaves
        the console's RTA pointed wherever it already is."""
        correlation_id = correlation_id or self.diagnostics.new_correlation_id()
        if self.running:
            raise RuntimeError("RTA streamer already running")

        if source is not None:
            try:
                (self._original_source,) = self.osc.query(RTA_SOURCE_ADDRESS, correlation_id=correlation_id)
            except TimeoutError:
                # Can't snapshot -> don't change what we can't put back.
                self._original_source = None
                self.diagnostics.log_watchdog(
                    "rta_source_snapshot_unavailable",
                    {"detail": "console did not answer /-stat/rtasource; leaving RTA source untouched"},
                    correlation_id=correlation_id,
                )
            else:
                self.osc.send(RTA_SOURCE_ADDRESS, source, correlation_id=correlation_id)
                self._source_changed = True

        self.osc.add_address_listener(RTA_METER_PATH, self._queue)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="rta-streamer", daemon=True)
        self._thread.start()
        self.diagnostics.log_state_change(
            "rta_stream_started", after={"source": source}, correlation_id=correlation_id
        )

    def stop(self, correlation_id: str | None = None) -> None:
        correlation_id = correlation_id or self.diagnostics.new_correlation_id()
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.osc.remove_address_listener(RTA_METER_PATH, self._queue)
        if self._source_changed and self._original_source is not None:
            self.osc.send(RTA_SOURCE_ADDRESS, self._original_source, correlation_id=correlation_id)
            self._source_changed = False
        self.diagnostics.log_state_change("rta_stream_stopped", correlation_id=correlation_id)

    def _loop(self) -> None:
        next_renew = 0.0
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_renew:
                try:
                    self.osc.send(METERS_SUBSCRIBE_ADDRESS, RTA_METER_PATH)
                except OSError as exc:
                    self.diagnostics.log_error(exc, context="rta subscribe send failed")
                next_renew = now + self.renew_sec
            try:
                args = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            blob = next((a for a in args if isinstance(a, (bytes, bytearray))), None)
            if blob is None:
                continue
            try:
                bins = decode_rta_blob(bytes(blob))
            except MeterBlobError as exc:
                self.diagnostics.log_error(exc, context="rta blob decode failed")
                continue
            self.on_rta(bins)

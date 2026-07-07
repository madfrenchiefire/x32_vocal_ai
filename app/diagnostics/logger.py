"""Central structured event logger.

Every module in this app (OSC, MIDI, audio, web) logs through a shared
:class:`DiagnosticsLogger` instance instead of calling ``print()``. Events
are kept in a bounded in-memory ring buffer (fast reads for the debug
bundle / future live log view) and simultaneously appended as JSONL to a
per-session file on disk (durable history, survives a crash).

See CLAUDE.md ("Diagnostics event schema") for the field-by-field contract.
"""
from __future__ import annotations

import json
import threading
import traceback
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.diagnostics.models import Event, EventCategory


class DiagnosticsLogger:
    def __init__(
        self,
        log_dir: str | Path = "logs",
        ring_buffer_size: int = 10_000,
        state_provider: Callable[[], dict[str, Any]] | None = None,
        session_name: str | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._ring: deque[Event] = deque(maxlen=ring_buffer_size)
        self._seq = 0
        self._state_provider = state_provider
        self._listeners: list[Callable[[Event], None]] = []

        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.session_name = session_name or f"session_{uuid.uuid4().hex[:12]}"
        self.session_file = self.log_dir / f"{self.session_name}.jsonl"
        self._fh = self.session_file.open("a", encoding="utf-8")

    # -- correlation -------------------------------------------------
    @staticmethod
    def new_correlation_id() -> str:
        return uuid.uuid4().hex

    # -- live listeners (e.g. app.web.sockets broadcasting to clients) -----
    def add_listener(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    # -- core log path -------------------------------------------------
    def log(
        self,
        category: EventCategory,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> Event:
        with self._lock:
            self._seq += 1
            event = Event.create(self._seq, category, payload, correlation_id)
            self._ring.append(event)
            self._fh.write(json.dumps(event.to_dict(), default=str) + "\n")
            self._fh.flush()
            listeners = list(self._listeners)

        # Called outside the lock -- a listener that itself logs (e.g. a
        # broadcast failure) must not deadlock against this same lock.
        for listener in listeners:
            listener(event)
        return event

    # -- category convenience wrappers -------------------------------
    def log_osc_tx(self, address: str, args: tuple, correlation_id: str | None = None) -> Event:
        return self.log(EventCategory.OSC_TX, {"address": address, "args": list(args)}, correlation_id)

    def log_osc_rx(self, address: str, args: tuple, correlation_id: str | None = None) -> Event:
        return self.log(EventCategory.OSC_RX, {"address": address, "args": list(args)}, correlation_id)

    def log_midi_rx(
        self,
        raw_bytes: bytes,
        parsed: dict[str, Any],
        correlation_id: str | None = None,
    ) -> Event:
        return self.log(
            EventCategory.MIDI_RX,
            {"raw_bytes": list(raw_bytes), "parsed": parsed},
            correlation_id,
        )

    def log_user_action(self, action: str, details: dict[str, Any] | None = None) -> str:
        """Start a new cause-and-effect chain. Returns the correlation_id
        to attach to every OSC/MIDI/state event this action triggers."""
        correlation_id = self.new_correlation_id()
        self.log(EventCategory.USER_ACTION, {"action": action, "details": details or {}}, correlation_id)
        return correlation_id

    def log_state_change(
        self,
        description: str,
        before: Any = None,
        after: Any = None,
        correlation_id: str | None = None,
    ) -> Event:
        return self.log(
            EventCategory.STATE_CHANGE,
            {"description": description, "before": before, "after": after},
            correlation_id,
        )

    def log_watchdog(self, event: str, details: dict[str, Any] | None = None, correlation_id: str | None = None) -> Event:
        return self.log(EventCategory.WATCHDOG, {"event": event, "details": details or {}}, correlation_id)

    def log_error(
        self,
        exc: BaseException,
        context: str | None = None,
        correlation_id: str | None = None,
    ) -> Event:
        state_summary = self._state_provider() if self._state_provider else None
        payload = {
            "context": context,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            "state_summary": state_summary,
        }
        return self.log(EventCategory.ERROR, payload, correlation_id)

    # -- reads -----------------------------------------------------------
    def get_recent(self, n: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in list(self._ring)[-n:]]

    def get_all_ring(self) -> list[dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in self._ring]

    def summary_text(self, n: int = 50) -> str:
        """Human-readable rendering of the last n events, for summary.txt
        in the debug bundle."""
        lines = [f"Last {n} diagnostics events (session {self.session_name})", "=" * 60]
        for e in self.get_recent(n):
            lines.append(
                f"[{e['timestamp']}] seq={e['seq']} {e['category']} "
                f"corr={e['correlation_id']} :: {json.dumps(e['payload'], default=str)}"
            )
        return "\n".join(lines) + "\n"

    def close(self) -> None:
        with self._lock:
            self._fh.close()

    def __enter__(self) -> "DiagnosticsLogger":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

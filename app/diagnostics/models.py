"""Diagnostics event schema.

Every module in this app logs through :class:`app.diagnostics.logger.DiagnosticsLogger`
instead of ``print()``. See CLAUDE.md ("Diagnostics event schema") for the
on-disk JSONL contract -- treat that section as the source of truth and
keep it in sync with this file.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventCategory(str, Enum):
    OSC_TX = "osc_tx"
    OSC_RX = "osc_rx"
    MIDI_RX = "midi_rx"
    USER_ACTION = "user_action"
    STATE_CHANGE = "state_change"
    WATCHDOG = "watchdog"
    ERROR = "error"


def iso_timestamp_ms(t: float | None = None) -> str:
    """ISO-8601 timestamp with millisecond precision, UTC."""
    dt = datetime.fromtimestamp(t, tz=timezone.utc) if t is not None else datetime.now(timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Event:
    seq: int
    timestamp: str
    monotonic: float
    category: EventCategory
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "monotonic": self.monotonic,
            "category": self.category.value,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
        }

    @classmethod
    def create(
        cls,
        seq: int,
        category: EventCategory,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> "Event":
        return cls(
            seq=seq,
            timestamp=iso_timestamp_ms(),
            monotonic=time.monotonic(),
            category=category,
            payload=payload,
            correlation_id=correlation_id,
        )

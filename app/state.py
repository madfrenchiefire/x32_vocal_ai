"""Shared application state.

Single in-memory source of truth for what every service currently believes
about the console and the app's own runtime status. Services mutate their
own slice of state through the methods below (never by poking attributes
directly) so reads stay consistent across concurrent threads (OSC, MIDI,
audio analysis, and Flask/WebSocket request handlers).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class ConnectionState:
    connected: bool = False
    host: str | None = None
    port: int | None = None
    console_name: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    last_error: str | None = None
    last_seen_monotonic: float | None = None


@dataclass
class ChannelState:
    """Per-channel runtime state: routing/slot assignment (app.osc.routing_apply),
    MIDI hardware-control slot (app.midi.slots), and audio detection/filter
    settings (app.audio). `None` on the *_override fields means "use the
    matching AppConfig default", not "off"."""

    index: int
    card_out_slot: int | None = None
    midi_slot: int | None = None
    ai_enabled: bool = False
    # Whether this channel is currently routed through the app (Card return +
    # notch processing) vs bypassed back to its original snapshot source, or
    # never selected at all. False until app.osc.routing_apply.apply_routing
    # inserts it.
    inserted: bool = False
    active_notch_count: int = 0
    echo_cancellation_enabled: bool = False

    # Scribble-strip name/color as last read from the console
    # (app.osc.scribble_strip.read_all_channel_configs); None until read.
    scribble_name: str | None = None
    scribble_color: str | None = None

    # Per-channel overrides of AppConfig's global defaults; None = use the
    # global default.
    sensitivity: float = 0.5  # 0-1, heuristic detection threshold
    max_notches_override: int | None = None
    notch_depth_db_override: float | None = None
    notch_q_override: float | None = None
    mode: str = "live"  # "live" or "ring_out" -- see CLAUDE.md's Web UI modes


class AppState:
    """Thread-safe container for cross-service runtime state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.connection = ConnectionState()
        # Set by app.osc.routing_snapshot once a snapshot has been captured.
        self.current_snapshot: Any | None = None
        self.snapshot_history: list[str] = []
        # Set by app.osc.assign_set.snapshot_assign_sets once Set A/B has
        # been read -- the crash watchdog restores this alongside routing
        # (app.watchdog.Watchdog's assign_set_snapshot_provider).
        self.assign_set_snapshot: dict[str, tuple | None] | None = None
        self.channels: dict[int, ChannelState] = {i: ChannelState(index=i) for i in range(1, 33)}

    def update_connection(self, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                if not hasattr(self.connection, key):
                    raise AttributeError(f"ConnectionState has no field {key!r}")
                setattr(self.connection, key, value)

    def set_snapshot(self, snapshot: Any, saved_path: str | None = None) -> None:
        with self._lock:
            self.current_snapshot = snapshot
            if saved_path is not None:
                self.snapshot_history.append(saved_path)

    def set_assign_set_snapshot(self, snapshot: dict[str, tuple | None]) -> None:
        with self._lock:
            self.assign_set_snapshot = snapshot

    def apply_channel_configs(self, configs: dict[int, tuple | None]) -> None:
        """Update scribble_name/scribble_color from a
        app.osc.scribble_strip.read_all_channel_configs() result of
        (name, color_token) pairs. A field whose read timed out (None) is
        left untouched rather than being blanked out."""
        with self._lock:
            for channel, config in configs.items():
                if config is None or channel not in self.channels:
                    continue
                name, color = config
                if name is not None:
                    self.channels[channel].scribble_name = name
                if color is not None:
                    self.channels[channel].scribble_color = color

    def summary(self) -> dict:
        """Plain-dict snapshot of current state, safe to serialize.

        Used by DiagnosticsLogger for error context and by the debug
        bundle export.
        """
        with self._lock:
            return {
                "connection": vars(self.connection).copy(),
                "snapshot_history": list(self.snapshot_history),
                "has_current_snapshot": self.current_snapshot is not None,
                "has_assign_set_snapshot": self.assign_set_snapshot is not None,
                "channels": {i: vars(c).copy() for i, c in self.channels.items()},
            }

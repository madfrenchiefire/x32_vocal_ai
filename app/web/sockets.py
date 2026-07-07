"""WebSocket event handlers.

Every diagnostics event (osc_tx/rx, midi_rx, user_action, state_change,
watchdog, error) is broadcast to connected clients as it's logged, via
DiagnosticsLogger.add_listener -- this is what drives the web UI's live
event log and per-channel notch-placed updates without polling.

Channel meters are NOT the OSC /meters path CLAUDE.md originally
described -- that blob layout is still unconfirmed against real
hardware. Instead, app.audio.engine.AudioEngine measures RMS level per
Card slot directly from the audio it already has (the actual signal this
app is processing, which needs no console protocol confirmation at all)
and pushes {card_slot: level_dbfs} here via its on_levels_update hook,
broadcast as "channel_meters" every ~150ms.
"""
from __future__ import annotations

from flask_socketio import SocketIO

from app.audio.engine import AudioEngine
from app.diagnostics.logger import DiagnosticsLogger
from app.diagnostics.models import Event
from app.state import AppState


def register_socket_handlers(
    socketio: SocketIO,
    state: AppState,
    diagnostics: DiagnosticsLogger,
    audio_engine: AudioEngine | None = None,
) -> None:
    def _on_event(event: Event) -> None:
        socketio.emit("diagnostics_event", event.to_dict())

    diagnostics.add_listener(_on_event)

    if audio_engine is not None:
        audio_engine.on_levels_update = lambda levels: socketio.emit("channel_meters", levels)

    @socketio.on("connect")
    def _on_connect() -> None:
        diagnostics.log_state_change("websocket_client_connected")

    @socketio.on("disconnect")
    def _on_disconnect() -> None:
        diagnostics.log_state_change("websocket_client_disconnected")

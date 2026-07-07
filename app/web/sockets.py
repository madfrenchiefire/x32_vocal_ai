"""WebSocket event handlers.

Every diagnostics event (osc_tx/rx, midi_rx, user_action, state_change,
watchdog, error) is broadcast to connected clients as it's logged, via
DiagnosticsLogger.add_listener -- this is what drives the web UI's live
event log and per-channel notch-placed updates without polling. Channel
meters (/meters binary blobs) are not wired up yet -- CLAUDE.md flags the
blob layout itself as unconfirmed against real hardware.
"""
from __future__ import annotations

from flask_socketio import SocketIO

from app.diagnostics.logger import DiagnosticsLogger
from app.diagnostics.models import Event
from app.state import AppState


def register_socket_handlers(socketio: SocketIO, state: AppState, diagnostics: DiagnosticsLogger) -> None:
    def _on_event(event: Event) -> None:
        socketio.emit("diagnostics_event", event.to_dict())

    diagnostics.add_listener(_on_event)

    @socketio.on("connect")
    def _on_connect() -> None:
        diagnostics.log_state_change("websocket_client_connected")

    @socketio.on("disconnect")
    def _on_disconnect() -> None:
        diagnostics.log_state_change("websocket_client_disconnected")

    # Future events (not yet implemented):
    #   - "channel_meters"  streamed per-channel level data from /meters
    #     (blob layout unconfirmed against real hardware -- see CLAUDE.md)

"""WebSocket event handlers.

Only connection bookkeeping is wired up this phase. Later phases add
events here: live per-channel meters, notch-placed/removed events, MIDI
slot assignment changes, connection status changes -- all should be
pushed to clients as they happen rather than polled.
"""
from __future__ import annotations

from flask_socketio import SocketIO

from app.diagnostics.logger import DiagnosticsLogger
from app.state import AppState


def register_socket_handlers(socketio: SocketIO, state: AppState, diagnostics: DiagnosticsLogger) -> None:
    @socketio.on("connect")
    def _on_connect() -> None:
        diagnostics.log_state_change("websocket_client_connected")

    @socketio.on("disconnect")
    def _on_disconnect() -> None:
        diagnostics.log_state_change("websocket_client_disconnected")

    # Future events (not yet implemented):
    #   - "channel_meters"     streamed per-channel level data from /meters
    #   - "notch_event"        notch placed/removed by the analysis thread
    #   - "connection_status"  pushed on OscConnection connect/disconnect
    #   - "midi_slot_update"   pushed on MidiService slot assign/release

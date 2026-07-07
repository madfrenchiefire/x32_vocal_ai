"""Flask + WebSocket application factory.

Wires the shared AppState/AppConfig/DiagnosticsLogger (and, once
connected/started, the OscConnection/AudioEngine/MidiService) into a
Flask app so routes and socket handlers can reach them via
app.extensions. Every diagnostics event also gets pushed to connected
WebSocket clients live (app.web.sockets), so the event log and per-channel
notch placements update in real time instead of being polled.
"""
from __future__ import annotations

from flask import Flask
from flask_socketio import SocketIO

from app.audio.engine import AudioEngine
from app.config import AppConfig
from app.diagnostics.logger import DiagnosticsLogger
from app.midi.service import MidiService
from app.osc.connection import OscConnection
from app.state import AppState
from app.web.routes import bp as main_bp
from app.web.sockets import register_socket_handlers


def create_app(
    config: AppConfig,
    state: AppState,
    diagnostics: DiagnosticsLogger,
    osc: OscConnection | None = None,
    audio_engine: AudioEngine | None = None,
    midi_service: MidiService | None = None,
    config_path: str | None = None,
) -> tuple[Flask, SocketIO]:
    app = Flask(__name__)
    app.extensions["app_config"] = config
    app.extensions["app_state"] = state
    app.extensions["diagnostics"] = diagnostics
    app.extensions["osc_connection"] = osc
    app.extensions["audio_engine"] = audio_engine
    app.extensions["midi_service"] = midi_service
    # Device selections are persisted here if set (app.web.routes'
    # /api/devices/select) -- None means "update the in-memory config for
    # this run only", so tests and ad-hoc create_app() callers never write
    # a stray config.json into the working directory.
    app.extensions["config_path"] = config_path

    app.register_blueprint(main_bp)

    socketio = SocketIO(app, async_mode="threading")
    register_socket_handlers(socketio, state, diagnostics)

    return app, socketio


def run(
    config: AppConfig,
    state: AppState,
    diagnostics: DiagnosticsLogger,
    osc: OscConnection | None = None,
    audio_engine: AudioEngine | None = None,
    midi_service: MidiService | None = None,
    config_path: str | None = None,
) -> None:
    app, socketio = create_app(config, state, diagnostics, osc, audio_engine, midi_service, config_path)
    socketio.run(app, host=config.web_host, port=config.web_port)

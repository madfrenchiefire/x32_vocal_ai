"""Flask + WebSocket application factory.

Wires the shared AppState/AppConfig/DiagnosticsLogger into a Flask app so
routes and socket handlers can reach them via app.extensions. This phase
only registers the diagnostics export route (app.web.routes) and basic
connection bookkeeping over WebSocket (app.web.sockets) -- the routing
grid, per-channel controls, and live meters described in CLAUDE.md's
"Web UI" section are later-phase work.
"""
from __future__ import annotations

from flask import Flask
from flask_socketio import SocketIO

from app.config import AppConfig
from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import OscConnection
from app.state import AppState
from app.web.routes import bp as main_bp
from app.web.sockets import register_socket_handlers


def create_app(
    config: AppConfig,
    state: AppState,
    diagnostics: DiagnosticsLogger,
    osc: OscConnection | None = None,
) -> tuple[Flask, SocketIO]:
    app = Flask(__name__)
    app.extensions["app_config"] = config
    app.extensions["app_state"] = state
    app.extensions["diagnostics"] = diagnostics
    app.extensions["osc_connection"] = osc

    app.register_blueprint(main_bp)

    socketio = SocketIO(app, async_mode="threading")
    register_socket_handlers(socketio, state, diagnostics)

    return app, socketio


def run(config: AppConfig, state: AppState, diagnostics: DiagnosticsLogger, osc: OscConnection | None = None) -> None:
    app, socketio = create_app(config, state, diagnostics, osc)
    socketio.run(app, host=config.web_host, port=config.web_port)

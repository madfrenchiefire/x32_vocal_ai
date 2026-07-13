from __future__ import annotations

import socket
import threading

import pytest
from pythonosc.osc_message import OscMessage
from pythonosc.osc_message_builder import OscMessageBuilder

from app.diagnostics.logger import DiagnosticsLogger
from app.state import AppState


class FakeX32:
    """Minimal fake console: replies to /xinfo with a configurable firmware
    version and to any address pre-registered in extra_responses. Anything
    else gets no reply at all, mirroring how a real console ignores an
    unrecognized OSC address -- used to exercise the "unverified address"
    path without needing real hardware.

    Also supports real get/set semantics matching the X32 OSC protocol: a
    message with args is a "set" -- it's stored into extra_responses and
    echoed back (mirroring the console confirming a write) -- and a
    message with no args is a "get", answered from extra_responses if
    present. This lets write-path tests just query() before and after a
    send() and see the fake console's state actually change."""

    def __init__(self, version: str = "4.06-16") -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.version = version
        self.respond = True
        self.extra_responses: dict[str, tuple] = {}
        self._running = threading.Event()
        self._running.set()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while self._running.is_set():
            try:
                data, addr = self.sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                continue
            if not self.respond:
                continue
            msg = OscMessage(data)
            reply_args = None
            if msg.address == "/xinfo":
                reply_args = ("127.0.0.1", "TESTX32", "X32", self.version)
            elif msg.address == "/save":
                # Mirrors the real console's dataset-save acknowledgement:
                # /save ,siss scene <slot> <name> <note> -> /save scene 1.
                # Saved scenes are recorded for tests to inspect.
                self.saved_scenes = getattr(self, "saved_scenes", [])
                self.saved_scenes.append(tuple(msg.params))
                reply_args = (msg.params[0], 1)
            elif msg.params:
                # A "set": store it, then echo back the new value (mirrors
                # a real console confirming a write).
                self.extra_responses[msg.address] = tuple(msg.params)
                reply_args = tuple(msg.params)
            elif msg.address in self.extra_responses:
                reply_args = self.extra_responses[msg.address]
            if reply_args is not None:
                builder = OscMessageBuilder(address=msg.address)
                for a in reply_args:
                    builder.add_arg(a)
                self.sock.sendto(builder.build().dgram, addr)

    def close(self) -> None:
        self._running.clear()
        self.sock.close()
        self._thread.join(timeout=1)


@pytest.fixture
def fake_x32():
    server = FakeX32()
    yield server
    server.close()


@pytest.fixture
def app_state() -> AppState:
    return AppState()


@pytest.fixture
def diagnostics(tmp_path, app_state) -> DiagnosticsLogger:
    logger = DiagnosticsLogger(log_dir=tmp_path / "logs", ring_buffer_size=1000, state_provider=app_state.summary)
    yield logger
    logger.close()

from __future__ import annotations

import time

import pytest

from app.osc import connection as connection_module
from app.osc.connection import FirmwareTooOldError, OscConnection, OscConnectionError


def make_connection(fake_x32, diagnostics, app_state, **kwargs) -> OscConnection:
    defaults = dict(
        host="127.0.0.1",
        port=fake_x32.port,
        diagnostics=diagnostics,
        state=app_state,
        timeout_sec=1.0,
        xremote_interval_sec=0.2,
        min_firmware="4.0",
        reconnect_backoff_sec=(0.1,),
    )
    defaults.update(kwargs)
    return OscConnection(**defaults)


def test_connect_success_populates_xinfo_and_state(fake_x32, diagnostics, app_state):
    osc = make_connection(fake_x32, diagnostics, app_state)
    try:
        osc.connect()
        assert osc.connected is True
        assert osc.xinfo["model"] == "X32"
        assert osc.xinfo["version"] == "4.06-16"
        assert app_state.connection.connected is True
        assert app_state.connection.firmware_version == "4.06-16"
    finally:
        osc.close()


def test_connect_rejects_old_firmware(fake_x32, diagnostics, app_state):
    fake_x32.version = "3.09-1"
    osc = make_connection(fake_x32, diagnostics, app_state)
    with pytest.raises(FirmwareTooOldError):
        osc.connect()
    osc.close()


def test_connect_times_out_with_no_response(diagnostics, app_state):
    # Nothing listening on this port.
    osc = OscConnection(
        host="127.0.0.1",
        port=1,
        diagnostics=diagnostics,
        state=app_state,
        timeout_sec=0.3,
    )
    with pytest.raises(OscConnectionError):
        osc.connect()
    osc.close()


def test_query_many_returns_none_for_unanswered_addresses(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses["/foo"] = (1, 2)
    fake_x32.extra_responses["/bar"] = ("baz",)

    osc = make_connection(fake_x32, diagnostics, app_state)
    osc.connect()
    try:
        results = osc.query_many(["/foo", "/bar", "/missing"], timeout=0.3, retries=0)
        assert results["/foo"] == (1, 2)
        assert results["/bar"] == ("baz",)
        assert results["/missing"] is None
    finally:
        osc.close()


def test_watchdog_detects_loss_and_reconnects(monkeypatch, fake_x32, diagnostics, app_state):
    monkeypatch.setattr(connection_module, "WATCHDOG_TICK_SEC", 0.05)
    osc = make_connection(fake_x32, diagnostics, app_state, xremote_interval_sec=0.1)
    try:
        osc.connect()
        assert osc.connected is True

        fake_x32.respond = False
        deadline = time.monotonic() + 2.0
        while osc.connected and time.monotonic() < deadline:
            time.sleep(0.05)
        assert osc.connected is False

        fake_x32.respond = True
        deadline = time.monotonic() + 2.0
        while not osc.connected and time.monotonic() < deadline:
            time.sleep(0.05)
        assert osc.connected is True
    finally:
        osc.close()

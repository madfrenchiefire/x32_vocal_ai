from __future__ import annotations

import time

from app.osc.channel_eq import (
    ChannelEqError,
    eq_band_addr,
    eq_on_addr,
    write_notches_to_console_eq,
)
from app.osc.connection import OscConnection
from app.osc.console_eq_sync import ConsoleEqSync, _signature


def _make_osc(fake_x32, diagnostics, app_state) -> OscConnection:
    osc = OscConnection(
        host="127.0.0.1",
        port=fake_x32.port,
        diagnostics=diagnostics,
        state=app_state,
        timeout_sec=1.0,
        xremote_interval_sec=0.5,
    )
    osc.connect()
    return osc


def _register_default_console_eq(fake_x32, channel: int) -> None:
    fake_x32.extra_responses[eq_on_addr(channel)] = (0,)
    for band in range(1, 5):
        fake_x32.extra_responses[eq_band_addr(channel, band, "type")] = (2,)
        for param in ("f", "g", "q"):
            fake_x32.extra_responses[eq_band_addr(channel, band, param)] = (0.5,)


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


NOTCHES = [{"frequency_hz": 1200.0, "depth_db": -12.0, "q": 8.0}]


def test_write_notches_does_not_snapshot(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        written = write_notches_to_console_eq(osc, diagnostics, 5, NOTCHES)
    finally:
        osc.close()
    assert written[0]["frequency_hz"] == 1200.0
    assert fake_x32.extra_responses[eq_on_addr(5)] == (1,)


def test_write_notches_empty_raises(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        import pytest

        with pytest.raises(ChannelEqError):
            write_notches_to_console_eq(osc, diagnostics, 5, [])
    finally:
        osc.close()


def test_signature_is_order_independent():
    a = [{"frequency_hz": 100.0, "depth_db": -6.0, "q": 4.0},
         {"frequency_hz": 200.0, "depth_db": -9.0, "q": 5.0}]
    b = list(reversed(a))
    assert _signature(a) == _signature(b)


def test_sync_writes_notches_for_internal_channel(fake_x32, diagnostics, app_state):
    _register_default_console_eq(fake_x32, 3)
    app_state.channels[3].eq_mode = "internal"
    osc = _make_osc(fake_x32, diagnostics, app_state)
    sync = ConsoleEqSync(osc=osc, diagnostics=diagnostics, state=app_state, notches_provider=lambda ch: NOTCHES)
    sync.start()
    try:
        sync.request_sync(3)
        assert _wait_for(lambda: fake_x32.extra_responses.get(eq_on_addr(3)) == (1,))
        # The channel's pre-write EQ was snapshotted for restore.
        assert 3 in app_state.console_eq_snapshots
    finally:
        sync.stop()
        osc.close()


def test_sync_ignores_external_channel(fake_x32, diagnostics, app_state):
    _register_default_console_eq(fake_x32, 4)
    app_state.channels[4].eq_mode = "external"  # default
    osc = _make_osc(fake_x32, diagnostics, app_state)
    sync = ConsoleEqSync(osc=osc, diagnostics=diagnostics, state=app_state, notches_provider=lambda ch: NOTCHES)
    sync.start()
    try:
        sync.request_sync(4)
        time.sleep(0.3)
        assert fake_x32.extra_responses[eq_on_addr(4)] == (0,)  # untouched
        assert 4 not in app_state.console_eq_snapshots
    finally:
        sync.stop()
        osc.close()


def test_sync_restores_when_notches_clear(fake_x32, diagnostics, app_state):
    _register_default_console_eq(fake_x32, 6)
    app_state.channels[6].eq_mode = "internal"
    osc = _make_osc(fake_x32, diagnostics, app_state)

    current = {"notches": NOTCHES}
    sync = ConsoleEqSync(
        osc=osc, diagnostics=diagnostics, state=app_state, notches_provider=lambda ch: current["notches"]
    )
    sync.start()
    try:
        sync.request_sync(6)
        assert _wait_for(lambda: fake_x32.extra_responses.get(eq_on_addr(6)) == (1,))

        # Feedback gone: the next sync restores the original EQ (eq/on 0).
        current["notches"] = []
        sync.request_sync(6)
        assert _wait_for(lambda: fake_x32.extra_responses.get(eq_on_addr(6)) == (0,))
    finally:
        sync.stop()
        osc.close()


def test_restore_channel_puts_eq_back(fake_x32, diagnostics, app_state):
    _register_default_console_eq(fake_x32, 7)
    app_state.channels[7].eq_mode = "internal"
    osc = _make_osc(fake_x32, diagnostics, app_state)
    sync = ConsoleEqSync(osc=osc, diagnostics=diagnostics, state=app_state, notches_provider=lambda ch: NOTCHES)
    sync.start()
    try:
        sync.request_sync(7)
        assert _wait_for(lambda: fake_x32.extra_responses.get(eq_on_addr(7)) == (1,))

        assert sync.restore_channel(7) is True
        # Snapshot had eq/on = 0, so restore writes it back off.
        assert _wait_for(lambda: fake_x32.extra_responses.get(eq_on_addr(7)) == (0,))
    finally:
        sync.stop()
        osc.close()

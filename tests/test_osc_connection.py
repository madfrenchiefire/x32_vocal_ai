from __future__ import annotations

import threading
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


def test_query_many_unanswered_address_does_not_starve_later_addresses(fake_x32, diagnostics, app_state):
    # Regression test: an address with no reply used to eat the whole
    # deadline via a blocking get(timeout=remaining), which meant every
    # address *after* it in the list got reported None even though its
    # reply had already arrived -- only reproduces with the missing
    # address positioned before others, not last (see
    # test_query_many_returns_none_for_unanswered_addresses above).
    fake_x32.extra_responses["/a"] = (1,)
    fake_x32.extra_responses["/c"] = (3,)

    osc = make_connection(fake_x32, diagnostics, app_state)
    osc.connect()
    try:
        results = osc.query_many(["/a", "/missing", "/c"], timeout=0.3, retries=0)
        assert results["/a"] == (1,)
        assert results["/missing"] is None
        assert results["/c"] == (3,)
    finally:
        osc.close()


def test_query_until_match_returns_immediately_when_already_correct(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses["/foo"] = (5,)
    osc = make_connection(fake_x32, diagnostics, app_state)
    osc.connect()
    try:
        start = time.monotonic()
        value = osc.query_until_match("/foo", 5, attempts=5, delay_sec=0.2)
        assert value == 5
        assert time.monotonic() - start < 0.2  # no retry delay needed
    finally:
        osc.close()


def test_query_until_match_gives_up_after_attempts_exhausted(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses["/foo"] = (0,)
    osc = make_connection(fake_x32, diagnostics, app_state)
    osc.connect()
    try:
        value = osc.query_until_match("/foo", 5, attempts=3, delay_sec=0.05)
        assert value == 0  # never settled -- last value seen, not the expected one
    finally:
        osc.close()


def test_query_until_match_retries_through_a_settling_write(fake_x32, diagnostics, app_state):
    # Reproduces the real-hardware finding: a value that's stale on the
    # first read or two, then settles to the expected value shortly after,
    # without any other action taken.
    fake_x32.extra_responses["/foo"] = (0,)
    osc = make_connection(fake_x32, diagnostics, app_state)
    osc.connect()
    try:
        def settle_after_delay():
            time.sleep(0.1)
            fake_x32.extra_responses["/foo"] = (5,)

        threading.Thread(target=settle_after_delay, daemon=True).start()

        value = osc.query_until_match("/foo", 5, attempts=10, delay_sec=0.05)
        assert value == 5
    finally:
        osc.close()


def test_watchdog_does_not_flag_an_idle_but_alive_console_as_disconnected(monkeypatch, fake_x32, diagnostics, app_state):
    # /xremote itself gets no reply -- an idle console (nothing changed,
    # so nothing pushed back) legitimately produces no incoming traffic
    # for a while. The watchdog must confirm with an active /xinfo probe
    # (which the fake console still answers) before ever declaring the
    # connection lost, rather than treating quiet time alone as a
    # disconnect.
    monkeypatch.setattr(connection_module, "WATCHDOG_TICK_SEC", 0.05)
    osc = make_connection(fake_x32, diagnostics, app_state, xremote_interval_sec=0.1)
    try:
        osc.connect()
        assert osc.connected is True

        # Outlast the stale threshold (xremote_interval_sec * 2.5 = 0.25s)
        # several times over, with the fake console still answering.
        time.sleep(1.0)
        assert osc.connected is True

        events = [e for e in diagnostics.get_recent(200) if e["category"] == "watchdog"]
        assert not any(e["payload"]["event"] == "connection_lost" for e in events)
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

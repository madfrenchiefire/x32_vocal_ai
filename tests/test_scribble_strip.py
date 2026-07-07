from __future__ import annotations

from app.osc.connection import OscConnection
from app.osc.scribble_strip import (
    channel_config_addr,
    read_all_channel_configs,
    read_channel_config,
    restore_channel_scribble,
    write_channel_scribble,
)


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


def test_write_channel_scribble_preserves_icon_and_source(fake_x32, diagnostics, app_state):
    addr = channel_config_addr(1)
    fake_x32.extra_responses[addr] = ("Ruby Vocal", 51, "YE", 1)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        write_channel_scribble(osc, diagnostics, 1, name="Ruby Vocal [AI]", color="GN")
        current = read_channel_config(osc, 1)
    finally:
        osc.close()

    assert current == ("Ruby Vocal [AI]", 51, "GN", 1)


def test_write_channel_scribble_partial_update_keeps_other_field(fake_x32, diagnostics, app_state):
    addr = channel_config_addr(2)
    fake_x32.extra_responses[addr] = ("Left Vocal", 50, "YE", 2)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        write_channel_scribble(osc, diagnostics, 2, color="RD")  # name not touched
        current = read_channel_config(osc, 2)
    finally:
        osc.close()

    assert current == ("Left Vocal", 50, "RD", 2)


def test_restore_channel_scribble_writes_exact_tuple(fake_x32, diagnostics, app_state):
    addr = channel_config_addr(3)
    fake_x32.extra_responses[addr] = ("Center Vocal [BYPASS]", 50, "RD", 3)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        restore_channel_scribble(osc, diagnostics, 3, ("Center Vocal", 50, "YE", 3))
        current = read_channel_config(osc, 3)
    finally:
        osc.close()

    assert current == ("Center Vocal", 50, "YE", 3)


def test_read_all_channel_configs_returns_all_32_by_channel_number(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses[channel_config_addr(1)] = ("Ruby Vocal", 51, "YE", 1)
    fake_x32.extra_responses[channel_config_addr(9)] = ("Left Vocal", 50, "RD", 9)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        configs = read_all_channel_configs(osc)
    finally:
        osc.close()

    assert configs[1] == ("Ruby Vocal", 51, "YE", 1)
    assert configs[9] == ("Left Vocal", 50, "RD", 9)
    assert len(configs) == 32
    assert configs[2] is None  # never registered on the fake console -- timed out

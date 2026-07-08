from __future__ import annotations

from app.osc.connection import OscConnection
from app.osc.scribble_strip import (
    channel_color_addr,
    channel_name_addr,
    decode_color,
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


def test_decode_color_maps_int_enum_and_passes_tokens_through():
    assert decode_color(3) == "YE"
    assert decode_color(11) == "YEi"
    assert decode_color("RD") == "RD"
    assert decode_color(99) == "UNKNOWN(99)"
    assert decode_color(None) is None


def test_write_channel_scribble_writes_leaf_addresses(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses[channel_name_addr(1)] = ("Ruby Vocal",)
    fake_x32.extra_responses[channel_color_addr(1)] = (3,)  # YE

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        write_channel_scribble(osc, diagnostics, 1, name="Ruby Vocal [AI]", color="GN")
        current = read_channel_config(osc, 1)
    finally:
        osc.close()

    assert current == ("Ruby Vocal [AI]", 2)  # GN written as its raw enum int


def test_write_channel_scribble_partial_update_keeps_other_field(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses[channel_name_addr(2)] = ("Left Vocal",)
    fake_x32.extra_responses[channel_color_addr(2)] = (3,)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        write_channel_scribble(osc, diagnostics, 2, color="RD")  # name not touched
        current = read_channel_config(osc, 2)
    finally:
        osc.close()

    assert current == ("Left Vocal", 1)


def test_restore_channel_scribble_writes_captured_raw_values(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses[channel_name_addr(3)] = ("Center Vocal [BYPASS]",)
    fake_x32.extra_responses[channel_color_addr(3)] = (1,)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        restore_channel_scribble(osc, diagnostics, 3, ("Center Vocal", 3))
        current = read_channel_config(osc, 3)
    finally:
        osc.close()

    assert current == ("Center Vocal", 3)


def test_read_all_channel_configs_returns_all_32_by_channel_number(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses[channel_name_addr(1)] = ("Ruby Vocal",)
    fake_x32.extra_responses[channel_color_addr(1)] = (3,)
    fake_x32.extra_responses[channel_name_addr(9)] = ("Left Vocal",)
    # channel 9's color never answers -- name still comes through

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        configs = read_all_channel_configs(osc)
    finally:
        osc.close()

    assert configs[1] == ("Ruby Vocal", "YE")
    assert configs[9] == ("Left Vocal", None)
    assert len(configs) == 32
    assert configs[2] == (None, None)  # never registered on the fake console -- timed out

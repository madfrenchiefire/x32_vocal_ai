from __future__ import annotations

import threading
import time

from app.osc import addresses
from app.osc.assign_set import all_assign_set_addresses
from app.osc.connection import OscConnection
from app.osc.protocol_discovery import (
    capture_full_state,
    capture_meters_sample,
    sniff_pushed_changes,
    watch_until_changed,
)
from app.osc.scribble_strip import channel_config_addr


def _make_osc(fake_x32, diagnostics, app_state, **kwargs) -> OscConnection:
    defaults = dict(
        host="127.0.0.1",
        port=fake_x32.port,
        diagnostics=diagnostics,
        state=app_state,
        timeout_sec=1.0,
        xremote_interval_sec=0.5,
    )
    defaults.update(kwargs)
    osc = OscConnection(**defaults)
    osc.connect()
    return osc


def _populate_full_state(fake_x32) -> None:
    for i, addr in enumerate(addresses.ALL_USERROUT_IN):
        fake_x32.extra_responses[addr] = (i,)
    for i, addr in enumerate(addresses.ALL_USERROUT_OUT):
        fake_x32.extra_responses[addr] = (i + 100,)
    for group, addr_table_pairs in addresses.ROUTING_GROUPS.items():
        for i, (addr, _table) in enumerate(addr_table_pairs):
            fake_x32.extra_responses[addr] = (i,)
    for addr in all_assign_set_addresses():
        fake_x32.extra_responses[addr] = (0,)
    for ch in range(1, 33):
        fake_x32.extra_responses[channel_config_addr(ch)] = (f"Ch{ch}", 1, "GN", ch)


def test_capture_full_state_gathers_routing_assign_sets_and_scribble(fake_x32, diagnostics, app_state):
    _populate_full_state(fake_x32)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        result = capture_full_state(osc, diagnostics)
    finally:
        osc.close()

    assert result["xinfo"]["model"] == "X32"
    assert result["routing_snapshot"]["userrout_in"] == list(range(addresses.NUM_USERROUT_IN))
    assert result["assign_sets"][all_assign_set_addresses()[0]] == (0,)
    assert result["channel_configs"][1] == ("Ch1", 1, "GN", 1)
    assert result["channel_configs"][32] == ("Ch32", 1, "GN", 32)


def test_watch_until_changed_detects_a_change_mid_poll(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses["/probe"] = (1,)
    osc = _make_osc(fake_x32, diagnostics, app_state)

    def _flip_after_delay():
        time.sleep(0.2)
        fake_x32.extra_responses["/probe"] = (2,)

    try:
        threading.Thread(target=_flip_after_delay, daemon=True).start()
        changed = watch_until_changed(osc, ["/probe"], diagnostics, poll_interval_sec=0.05, timeout_sec=5.0)
    finally:
        osc.close()

    assert changed == {"/probe": {"before": (1,), "after": (2,)}}


def test_watch_until_changed_times_out_with_no_change(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses["/probe"] = (1,)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        changed = watch_until_changed(osc, ["/probe"], diagnostics, poll_interval_sec=0.05, timeout_sec=0.3)
    finally:
        osc.close()

    assert changed == {}


def test_watch_until_changed_calls_on_tick(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses["/probe"] = (1,)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    ticks: list = []
    try:
        watch_until_changed(
            osc, ["/probe"], diagnostics, poll_interval_sec=0.05, timeout_sec=0.2, on_tick=ticks.append,
        )
    finally:
        osc.close()

    assert len(ticks) > 0


def test_capture_meters_sample_records_whatever_arrives(fake_x32, diagnostics, app_state):
    # The subscribe goes to the parent /meters address with the blob path
    # as a string arg (documented form); the console then streams blobs on
    # the blob path itself. The fake console doesn't implement that
    # streaming, so simulate the console's push with a bare "get" on the
    # blob path from a side thread (the fake replies from
    # extra_responses, which reaches our listener just like a real push).
    fake_x32.extra_responses["/meters/1"] = (b"\x00\x01\x02\x03",)
    osc = _make_osc(fake_x32, diagnostics, app_state)

    def _simulate_console_push():
        time.sleep(0.1)
        osc.send("/meters/1")

    try:
        threading.Thread(target=_simulate_console_push, daemon=True).start()
        messages = capture_meters_sample(osc, diagnostics, meter_path="/meters/1", listen_sec=0.5)
    finally:
        osc.close()

    assert len(messages) == 1
    assert messages[0][0] == b"\x00\x01\x02\x03"
    # The subscribe itself must have gone to the parent /meters address
    # with the blob path as a string argument (the documented form) -- the
    # fake console records any message-with-args as a "set".
    assert fake_x32.extra_responses["/meters"] == ("/meters/1",)


def test_capture_meters_sample_empty_when_nothing_replies(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        messages = capture_meters_sample(osc, diagnostics, meter_path="/meters/1", listen_sec=0.2)
    finally:
        osc.close()

    assert messages == []


def test_sniff_pushed_changes_records_unsolicited_messages(fake_x32, diagnostics, app_state):
    # Simulates the console pushing a state change on an address this
    # project has never heard of -- the whole point of the sniffer vs
    # watch_until_changed's known-address polling.
    fake_x32.extra_responses["/some/unknown/address"] = ("MIDI CC 80", 1)
    osc = _make_osc(fake_x32, diagnostics, app_state)

    def _simulate_console_push():
        time.sleep(0.1)
        osc.send("/some/unknown/address")  # bare get -> fake replies, arrives like a push

    try:
        threading.Thread(target=_simulate_console_push, daemon=True).start()
        messages = sniff_pushed_changes(osc, diagnostics, duration_sec=0.5)
    finally:
        osc.close()

    assert ("/some/unknown/address", ("MIDI CC 80", 1)) in messages


def test_sniff_pushed_changes_empty_when_console_is_silent(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        messages = sniff_pushed_changes(osc, diagnostics, duration_sec=0.3)
    finally:
        osc.close()

    assert messages == []


def test_sniff_pushed_changes_calls_on_tick_and_unregisters(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    ticks: list = []
    try:
        sniff_pushed_changes(osc, diagnostics, duration_sec=0.3, on_tick=ticks.append)
        with osc._pending_lock:
            assert osc._sniffers == []
    finally:
        osc.close()

    assert len(ticks) > 0

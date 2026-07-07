from __future__ import annotations

import pytest

from app.osc.assign_set import (
    AssignSetError,
    all_assign_set_addresses,
    button_addr,
    encoder_addr,
    restore_assignments,
    snapshot_assign_sets,
    write_assignment,
)
from app.osc.connection import OscConnection


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


def test_encoder_and_button_addr_reject_set_c():
    with pytest.raises(ValueError):
        encoder_addr("C", 1)
    with pytest.raises(ValueError):
        button_addr("C", 1)


def test_encoder_and_button_addr_reject_out_of_range_index():
    with pytest.raises(ValueError):
        encoder_addr("A", 5)
    with pytest.raises(ValueError):
        button_addr("B", 4)  # buttons are numbered 5-12, continuing after the encoders
    with pytest.raises(ValueError):
        button_addr("B", 13)


def test_all_assign_set_addresses_covers_both_sets():
    addrs = all_assign_set_addresses()
    # Encoder shape confirmed by real-console sniff (2026-07-07); buttons
    # numbered 5-12 per the same userctrl tree.
    assert "/config/userctrl/A/enc/1" in addrs
    assert "/config/userctrl/B/btn/12" in addrs
    assert len(addrs) == 2 * (4 + 8)


def test_snapshot_assign_sets_reads_all_addresses(fake_x32, diagnostics, app_state):
    for addr in all_assign_set_addresses():
        fake_x32.extra_responses[addr] = ("PLACEHOLDER",)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        results = snapshot_assign_sets(osc, diagnostics)
    finally:
        osc.close()

    assert len(results) == 24
    assert results["/config/userctrl/A/enc/1"] == ("PLACEHOLDER",)


def test_write_assignment_verifies_readback(fake_x32, diagnostics, app_state):
    addr = encoder_addr("A", 1)
    fake_x32.extra_responses[addr] = ("OLD",)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        write_assignment(osc, diagnostics, addr, "NEW_VALUE")
        assert fake_x32.extra_responses[addr] == ("NEW_VALUE",)
    finally:
        osc.close()


def test_write_assignment_raises_on_mismatch(monkeypatch, fake_x32, diagnostics, app_state):
    addr = encoder_addr("A", 1)
    fake_x32.extra_responses[addr] = ("OLD",)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    monkeypatch.setattr(osc, "send", lambda *a, **k: None)

    try:
        with pytest.raises(AssignSetError):
            write_assignment(osc, diagnostics, addr, "NEW_VALUE")
    finally:
        osc.close()


def test_restore_assignments_replays_snapshot(fake_x32, diagnostics, app_state):
    addr1 = encoder_addr("A", 1)
    addr2 = button_addr("B", 5)
    fake_x32.extra_responses[addr1] = ("CHANGED",)
    fake_x32.extra_responses[addr2] = ("CHANGED",)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        mismatches = restore_assignments(osc, diagnostics, {addr1: ("ORIGINAL",), addr2: ("ORIGINAL",)})
    finally:
        osc.close()

    assert mismatches == []
    assert fake_x32.extra_responses[addr1] == ("ORIGINAL",)
    assert fake_x32.extra_responses[addr2] == ("ORIGINAL",)

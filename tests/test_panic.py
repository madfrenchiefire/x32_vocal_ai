from __future__ import annotations

import pytest

from app.osc.connection import OscConnection
from app.osc.panic import MUTED, UNMUTED, PanicError, channel_mix_on_addr, panic_mute, panic_restore


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


def test_panic_mutes_all_and_restore_preserves_pre_panic_states(fake_x32, diagnostics, app_state):
    # Channel 1 was live, channel 2 was ALREADY muted by the engineer.
    fake_x32.extra_responses[channel_mix_on_addr(1)] = (UNMUTED,)
    fake_x32.extra_responses[channel_mix_on_addr(2)] = (MUTED,)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        snapshot = panic_mute(osc, diagnostics, [1, 2])
        assert fake_x32.extra_responses[channel_mix_on_addr(1)] == (MUTED,)
        assert fake_x32.extra_responses[channel_mix_on_addr(2)] == (MUTED,)
        assert snapshot == {1: UNMUTED, 2: MUTED}

        panic_restore(osc, diagnostics, snapshot)
        import time
        deadline = time.monotonic() + 1.0
        while fake_x32.extra_responses[channel_mix_on_addr(1)] != (UNMUTED,) and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        osc.close()

    assert fake_x32.extra_responses[channel_mix_on_addr(1)] == (UNMUTED,)  # back on
    assert fake_x32.extra_responses[channel_mix_on_addr(2)] == (MUTED,)  # engineer's mute preserved


def test_panic_mutes_even_when_snapshot_read_times_out(fake_x32, diagnostics, app_state):
    # Channel 3's mix/on never answers -- it must still get muted, and its
    # snapshot entry must be None so restore leaves it alone.
    fake_x32.extra_responses[channel_mix_on_addr(1)] = (UNMUTED,)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        snapshot = panic_mute(osc, diagnostics, [1, 3])
        assert snapshot == {1: UNMUTED, 3: None}
        assert fake_x32.extra_responses[channel_mix_on_addr(3)] == (MUTED,)

        panic_restore(osc, diagnostics, snapshot)
        import time
        deadline = time.monotonic() + 1.0
        while fake_x32.extra_responses[channel_mix_on_addr(1)] != (UNMUTED,) and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        osc.close()

    assert fake_x32.extra_responses[channel_mix_on_addr(3)] == (MUTED,)  # left muted, not guessed


def test_panic_with_no_channels_raises(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        with pytest.raises(PanicError):
            panic_mute(osc, diagnostics, [])
    finally:
        osc.close()

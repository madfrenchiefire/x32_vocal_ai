from __future__ import annotations

import pytest

from app.osc.channel_eq import (
    EQ_TYPE_PEQ,
    ChannelEqError,
    commit_notches_to_console_eq,
    eq_band_addr,
    eq_on_addr,
    freq_to_normalized,
    gain_to_normalized,
    normalized_to_freq,
    normalized_to_gain,
    normalized_to_q,
    q_to_normalized,
    restore_console_eq,
    snapshot_console_eq,
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


def _register_default_eq(fake_x32, channel: int) -> None:
    fake_x32.extra_responses[eq_on_addr(channel)] = (0,)
    for band in range(1, 5):
        fake_x32.extra_responses[eq_band_addr(channel, band, "type")] = (2,)
        fake_x32.extra_responses[eq_band_addr(channel, band, "f")] = (0.5,)
        fake_x32.extra_responses[eq_band_addr(channel, band, "g")] = (0.5,)
        fake_x32.extra_responses[eq_band_addr(channel, band, "q")] = (0.3,)


# -- normalized conversions ---------------------------------------------------


def test_freq_conversion_endpoints_and_roundtrip():
    assert freq_to_normalized(20.0) == pytest.approx(0.0)
    assert freq_to_normalized(20000.0) == pytest.approx(1.0)
    # 632.456 Hz is the geometric midpoint of 20..20k -> exactly 0.5
    assert freq_to_normalized(632.4555) == pytest.approx(0.5, abs=1e-4)
    for hz in (60.0, 250.0, 1000.0, 4000.0, 12500.0):
        assert normalized_to_freq(freq_to_normalized(hz)) == pytest.approx(hz, rel=1e-6)


def test_gain_conversion_endpoints_and_roundtrip():
    assert gain_to_normalized(-15.0) == pytest.approx(0.0)
    assert gain_to_normalized(15.0) == pytest.approx(1.0)
    assert gain_to_normalized(0.0) == pytest.approx(0.5)
    assert gain_to_normalized(-20.0) == pytest.approx(0.0)  # clamped to console floor
    assert normalized_to_gain(gain_to_normalized(-12.0)) == pytest.approx(-12.0)


def test_q_conversion_is_inverted_log_scale():
    assert q_to_normalized(10.0) == pytest.approx(0.0)  # widest = normalized 0
    assert q_to_normalized(0.3) == pytest.approx(1.0)
    assert normalized_to_q(q_to_normalized(5.0)) == pytest.approx(5.0, rel=1e-6)
    assert q_to_normalized(99.0) == pytest.approx(0.0)  # clamped into range


# -- snapshot / commit / restore ----------------------------------------------


def test_commit_writes_bands_deepest_first_and_enables_eq(fake_x32, diagnostics, app_state):
    _register_default_eq(fake_x32, 1)
    notches = [
        {"id": 1, "frequency_hz": 2500.0, "q": 10.0, "depth_db": -6.0},
        {"id": 2, "frequency_hz": 315.0, "q": 10.0, "depth_db": -12.0},
    ]

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        result = commit_notches_to_console_eq(osc, diagnostics, 1, notches)
    finally:
        osc.close()

    assert [w["frequency_hz"] for w in result["written"]] == [315.0, 2500.0]  # low->high band order
    assert fake_x32.extra_responses[eq_on_addr(1)] == (1,)
    assert fake_x32.extra_responses[eq_band_addr(1, 1, "type")] == (EQ_TYPE_PEQ,)
    assert fake_x32.extra_responses[eq_band_addr(1, 1, "f")][0] == pytest.approx(freq_to_normalized(315.0))
    assert fake_x32.extra_responses[eq_band_addr(1, 1, "g")][0] == pytest.approx(gain_to_normalized(-12.0))
    assert fake_x32.extra_responses[eq_band_addr(1, 2, "f")][0] == pytest.approx(freq_to_normalized(2500.0))
    # Snapshot captured the pre-write state for restore.
    assert result["snapshot"][eq_on_addr(1)] == 0


def test_commit_takes_only_deepest_four_and_clamps_gain(fake_x32, diagnostics, app_state):
    _register_default_eq(fake_x32, 2)
    notches = [
        {"id": i, "frequency_hz": 100.0 * (i + 1), "q": 10.0, "depth_db": depth}
        for i, depth in enumerate([-6.0, -18.0, -9.0, -12.0, -3.0])  # -3dB one must be dropped
    ]

    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        result = commit_notches_to_console_eq(osc, diagnostics, 2, notches)
    finally:
        osc.close()

    depths = [w["gain_db"] for w in result["written"]]
    assert len(result["written"]) == 4
    assert -3.0 not in depths  # shallowest notch dropped
    clamped = [w for w in result["written"] if w["gain_clamped"]]
    assert len(clamped) == 1 and clamped[0]["gain_db"] == -15.0  # -18dB clamped to console floor


def test_commit_with_no_notches_raises(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        with pytest.raises(ChannelEqError):
            commit_notches_to_console_eq(osc, diagnostics, 1, [])
    finally:
        osc.close()


def test_commit_readback_failure_raises_with_snapshot_attached(monkeypatch, fake_x32, diagnostics, app_state):
    _register_default_eq(fake_x32, 3)
    osc = _make_osc(fake_x32, diagnostics, app_state)

    # Drop writes (messages with args) so every readback mismatches.
    real_send = osc.send
    def send_dropping_writes(address, *args, **kwargs):
        if args:
            return
        return real_send(address, *args, **kwargs)
    monkeypatch.setattr(osc, "send", send_dropping_writes)

    try:
        with pytest.raises(ChannelEqError) as excinfo:
            commit_notches_to_console_eq(
                osc, diagnostics, 3, [{"id": 1, "frequency_hz": 1000.0, "q": 10.0, "depth_db": -9.0}]
            )
    finally:
        osc.close()

    assert excinfo.value.snapshot[eq_on_addr(3)] == 0


def test_restore_writes_snapshot_back_and_skips_none(fake_x32, diagnostics, app_state):
    _register_default_eq(fake_x32, 4)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        snapshot = snapshot_console_eq(osc, 4)
        # App then commits something...
        commit_notches_to_console_eq(
            osc, diagnostics, 4, [{"id": 1, "frequency_hz": 800.0, "q": 10.0, "depth_db": -9.0}]
        )
        assert fake_x32.extra_responses[eq_on_addr(4)] == (1,)

        snapshot[eq_band_addr(4, 3, "f")] = None  # simulate a timed-out read at snapshot time
        restore_console_eq(osc, diagnostics, 4, snapshot)
    finally:
        osc.close()

    assert fake_x32.extra_responses[eq_on_addr(4)] == (0,)
    assert fake_x32.extra_responses[eq_band_addr(4, 1, "f")] == (0.5,)

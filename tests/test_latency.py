from __future__ import annotations

import numpy as np
import pytest

from app.audio.latency import (
    LatencyMeasurementError,
    find_click_delay_samples,
    generate_click,
    measure_round_trip_latency,
)
from app.config import AppConfig
from app.osc import addresses
from app.osc.connection import OscConnection

SAMPLE_RATE = 48000


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


def _delayed_loopback(delay_samples: int, noise_amplitude: float = 0.0):
    """A fake run_playrec: returns the played signal delayed by
    delay_samples, as a real console loop would."""
    def run(config, playback_multi, in_channels):
        rng = np.random.default_rng(7)
        recording = rng.uniform(-1.0, 1.0, (len(playback_multi), in_channels)).astype(np.float32) * noise_amplitude
        source = playback_multi[:, np.argmax(np.abs(playback_multi).sum(axis=0))]
        if delay_samples < len(source):
            recording[delay_samples:, in_channels - 1] += source[: len(source) - delay_samples]
        return recording
    return run


# -- click detection math ------------------------------------------------------


def test_find_click_delay_exact():
    playback, click_start = generate_click(SAMPLE_RATE)
    for delay in (0, 1, 37, 480):
        recording = np.zeros_like(playback)
        recording[delay:] = playback[: len(playback) - delay]
        assert find_click_delay_samples(recording, playback, click_start) == delay


def test_find_click_delay_survives_attenuation_and_noise():
    playback, click_start = generate_click(SAMPLE_RATE)
    rng = np.random.default_rng(3)
    recording = rng.uniform(-1.0, 1.0, len(playback)).astype(np.float32) * 0.01
    delay = 250
    recording[delay:] += 0.2 * playback[: len(playback) - delay]
    assert find_click_delay_samples(recording, playback, click_start) == delay


def test_find_click_delay_raises_on_silence():
    playback, click_start = generate_click(SAMPLE_RATE)
    with pytest.raises(LatencyMeasurementError):
        find_click_delay_samples(np.zeros_like(playback), playback, click_start)


def test_find_click_delay_raises_on_pure_noise():
    playback, click_start = generate_click(SAMPLE_RATE)
    rng = np.random.default_rng(9)
    noise = rng.uniform(-1.0, 1.0, len(playback)).astype(np.float32)
    with pytest.raises(LatencyMeasurementError):
        find_click_delay_samples(noise, playback, click_start)


# -- full measurement against the fake console ---------------------------------


def _register_loopback_targets(fake_x32, in_slot: int) -> None:
    fake_x32.extra_responses[addresses.userrout_out_addr(in_slot)] = (0,)
    fake_x32.extra_responses[addresses.card_block_addr_for_slot(in_slot)] = (2,)


def test_measure_round_trip_routes_measures_and_restores(fake_x32, diagnostics, app_state):
    _register_loopback_targets(fake_x32, 32)
    config = AppConfig(audio_sample_rate=SAMPLE_RATE)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        result = measure_round_trip_latency(
            osc, diagnostics, config,
            out_card_slot=32, in_card_slot=32,
            run_playrec=_delayed_loopback(delay_samples=384, noise_amplitude=0.005),
        )
    finally:
        osc.close()

    assert result.round_trip_samples == 384
    assert result.round_trip_ms == pytest.approx(8.0)  # 384 / 48000
    assert result.includes_converters is False
    # Console routing restored to what it was before.
    assert fake_x32.extra_responses[addresses.userrout_out_addr(32)] == (0,)
    assert fake_x32.extra_responses[addresses.card_block_addr_for_slot(32)] == (2,)


def test_measure_round_trip_restores_routing_even_when_no_click_arrives(fake_x32, diagnostics, app_state):
    _register_loopback_targets(fake_x32, 16)
    config = AppConfig(audio_sample_rate=SAMPLE_RATE)
    osc = _make_osc(fake_x32, diagnostics, app_state)

    def silent_playrec(config_, playback_multi, in_channels):
        return np.zeros((len(playback_multi), in_channels), dtype=np.float32)

    try:
        with pytest.raises(LatencyMeasurementError):
            measure_round_trip_latency(
                osc, diagnostics, config,
                out_card_slot=8, in_card_slot=16,
                run_playrec=silent_playrec,
            )
    finally:
        osc.close()

    assert fake_x32.extra_responses[addresses.userrout_out_addr(16)] == (0,)
    assert fake_x32.extra_responses[addresses.card_block_addr_for_slot(16)] == (2,)


def test_measure_round_trip_refuses_without_snapshot(fake_x32, diagnostics, app_state):
    # Console never answers the snapshot reads -- must refuse to write.
    config = AppConfig(audio_sample_rate=SAMPLE_RATE)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        with pytest.raises(LatencyMeasurementError, match="snapshot"):
            measure_round_trip_latency(
                osc, diagnostics, config,
                run_playrec=_delayed_loopback(100),
            )
    finally:
        osc.close()


def test_card_loopback_address_helpers():
    assert addresses.card_in_userrout_out_value(1) == 129
    assert addresses.card_in_userrout_out_value(32) == 160
    assert addresses.card_block_addr_for_slot(12) == "/config/routing/CARD/9-16"
    assert addresses.user_out_card_block_value(1) == 26
    assert addresses.user_out_card_block_value(12) == 27
    assert addresses.user_out_card_block_value(32) == 29
    with pytest.raises(ValueError):
        addresses.card_in_userrout_out_value(33)

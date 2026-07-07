from __future__ import annotations

import numpy as np
import pytest

from app.audio.echo_cancellation import (
    MAIN_LR_USERROUT_OUT_VALUE,
    EchoCancellationError,
    EchoCanceller,
    auto_route_reference_signal,
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


def _noise(num_samples: int, amplitude: float = 1.0, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (amplitude * rng.uniform(-1.0, 1.0, num_samples)).astype(np.float32)


def _feed_blocks(canceller: EchoCanceller, mic: np.ndarray, reference: np.ndarray, block_size: int) -> np.ndarray:
    residual = np.empty_like(mic)
    for start in range(0, len(mic), block_size):
        end = start + block_size
        residual[start:end] = canceller.process(mic[start:end], reference[start:end])
    return residual


def test_pure_echo_is_progressively_cancelled():
    # Mic signal is a delayed, attenuated copy of the reference (a classic
    # acoustic echo) with no near-end talker -- residual energy should drop
    # sharply as the NLMS filter adapts to the echo path.
    reference = _noise(20000, amplitude=1.0, seed=1)
    delay = 20
    attenuation = 0.4
    mic = np.zeros_like(reference)
    mic[delay:] = attenuation * reference[:-delay]

    canceller = EchoCanceller(filter_length_taps=64, step_size=0.5)
    residual = _feed_blocks(canceller, mic, reference, block_size=64)

    early_energy = np.mean(residual[:1000] ** 2)
    late_energy = np.mean(residual[-1000:] ** 2)
    assert late_energy < early_energy * 0.05


def test_uncorrelated_signal_is_not_cancelled():
    # Mic signal uncorrelated with the reference (no real echo path) --
    # the filter shouldn't be able to invent cancellation; residual energy
    # should stay close to the original mic energy throughout.
    reference = _noise(10000, amplitude=1.0, seed=2)
    mic = _noise(10000, amplitude=1.0, seed=3)

    canceller = EchoCanceller(filter_length_taps=64, step_size=0.5)
    residual = _feed_blocks(canceller, mic, reference, block_size=64)

    mic_energy = np.mean(mic.astype(np.float64) ** 2)
    residual_energy = np.mean(residual[-1000:].astype(np.float64) ** 2)
    assert residual_energy > mic_energy * 0.5


def test_double_talk_freezes_adaptation():
    # A loud, uncorrelated burst layered on top of a genuine echo (mimicking
    # someone talking over playback) must not be allowed to corrupt weights
    # already adapted to the real echo path.
    reference = _noise(20000, amplitude=1.0, seed=4)
    delay = 10
    attenuation = 0.3
    mic = np.zeros_like(reference)
    mic[delay:] = attenuation * reference[:-delay]

    canceller = EchoCanceller(filter_length_taps=32, step_size=0.5, double_talk_energy_ratio=2.0)
    _feed_blocks(canceller, mic, reference, block_size=32)
    weights_before_double_talk = canceller._weights.copy()

    # Loud uncorrelated near-end burst -- mic energy now far exceeds what
    # the reference could plausibly explain via an attenuating echo path.
    burst = mic.copy()
    burst_start = 10000
    burst[burst_start:burst_start + 2000] += _noise(2000, amplitude=5.0, seed=5)
    _feed_blocks(canceller, burst, reference, block_size=32)

    np.testing.assert_allclose(canceller._weights, weights_before_double_talk, atol=1e-9)


def test_mismatched_block_lengths_raise():
    canceller = EchoCanceller(filter_length_taps=16)
    with pytest.raises(ValueError):
        canceller.process(np.zeros(10, dtype=np.float32), np.zeros(9, dtype=np.float32))


def test_auto_route_reference_signal_picks_free_card_slots_and_writes(fake_x32, diagnostics, app_state):
    for addr in addresses.ALL_USERROUT_OUT:
        fake_x32.extra_responses[addr] = (0,)
    app_state.channels[1].card_out_slot = 1  # already claimed by a mic channel -- must be skipped

    config = AppConfig()
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        slots = auto_route_reference_signal(osc, diagnostics, config, app_state)
    finally:
        osc.close()

    assert slots == (2, 3)
    assert config.echo_reference_card_channels == (2, 3)
    assert fake_x32.extra_responses[addresses.userrout_out_addr(2)] == (MAIN_LR_USERROUT_OUT_VALUE,)
    assert fake_x32.extra_responses[addresses.userrout_out_addr(3)] == (MAIN_LR_USERROUT_OUT_VALUE,)


def test_auto_route_reference_signal_is_idempotent(fake_x32, diagnostics, app_state):
    for addr in addresses.ALL_USERROUT_OUT:
        fake_x32.extra_responses[addr] = (0,)

    config = AppConfig()
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        first = auto_route_reference_signal(osc, diagnostics, config, app_state)
        second = auto_route_reference_signal(osc, diagnostics, config, app_state)
    finally:
        osc.close()

    assert first == second == (1, 2)


def test_auto_route_reference_signal_raises_when_not_enough_free_slots(monkeypatch, fake_x32, diagnostics, app_state):
    monkeypatch.setattr(addresses, "NUM_USERROUT_OUT", 2)
    app_state.channels[1].card_out_slot = 1  # only slot 2 left free -- need 2

    config = AppConfig()
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        with pytest.raises(EchoCancellationError):
            auto_route_reference_signal(osc, diagnostics, config, app_state)
    finally:
        osc.close()


def test_auto_route_reference_signal_raises_on_mismatch(monkeypatch, fake_x32, diagnostics, app_state):
    fake_x32.extra_responses[addresses.userrout_out_addr(1)] = (0,)
    fake_x32.extra_responses[addresses.userrout_out_addr(2)] = (0,)
    config = AppConfig()
    osc = _make_osc(fake_x32, diagnostics, app_state)
    monkeypatch.setattr(osc, "send", lambda *a, **k: None)

    try:
        with pytest.raises(EchoCancellationError):
            auto_route_reference_signal(osc, diagnostics, config, app_state)
    finally:
        osc.close()

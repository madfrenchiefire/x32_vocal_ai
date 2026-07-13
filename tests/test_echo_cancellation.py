from __future__ import annotations

import numpy as np
import pytest

from app.audio.echo_cancellation import (
    EchoCancellationError,
    EchoCanceller,
    auto_route_reference_signal,
    find_main_lr_outputs,
)
from app.config import AppConfig
from app.osc import addresses
from app.osc.connection import OscConnection

SAMPLE_RATE = 48000

# What auto_route_reference_signal writes when the fake console has the
# factory-default Out patch (Main L on Output 15, Main R on Output 16).
OUT15_TAP = addresses.output_userrout_out_value(15)  # 183
OUT16_TAP = addresses.output_userrout_out_value(16)  # 184


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


def _patch_main_lr_outputs(fake_x32, main_l_output: int = 15, main_r_output: int = 16) -> None:
    """Give the fake console an Out 1-16 patch: everything OFF except the
    given pair carrying Main L / Main R (the X32 factory default)."""
    for n in range(1, addresses.NUM_MAIN_OUTPUTS + 1):
        fake_x32.extra_responses[addresses.output_src_addr(n)] = (0,)
    fake_x32.extra_responses[addresses.output_src_addr(main_l_output)] = (addresses.OUTPUT_SRC_MAIN_L,)
    fake_x32.extra_responses[addresses.output_src_addr(main_r_output)] = (addresses.OUTPUT_SRC_MAIN_R,)


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
    _patch_main_lr_outputs(fake_x32)
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
    assert fake_x32.extra_responses[addresses.userrout_out_addr(2)] == (OUT15_TAP,)
    assert fake_x32.extra_responses[addresses.userrout_out_addr(3)] == (OUT16_TAP,)


def test_auto_route_reference_signal_is_idempotent(fake_x32, diagnostics, app_state):
    _patch_main_lr_outputs(fake_x32)
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
    _patch_main_lr_outputs(fake_x32)
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
    _patch_main_lr_outputs(fake_x32)
    fake_x32.extra_responses[addresses.userrout_out_addr(1)] = (0,)
    fake_x32.extra_responses[addresses.userrout_out_addr(2)] = (0,)
    config = AppConfig()
    osc = _make_osc(fake_x32, diagnostics, app_state)

    # Drop only the userrout *writes* (messages with args); queries still
    # go out, so the Main L/R output discovery succeeds and the failure
    # exercised here is specifically the readback mismatch.
    real_send = osc.send
    def send_dropping_writes(address, *args, **kwargs):
        if address.startswith("/config/userrout/out/") and args:
            return
        return real_send(address, *args, **kwargs)
    monkeypatch.setattr(osc, "send", send_dropping_writes)

    try:
        with pytest.raises(EchoCancellationError):
            auto_route_reference_signal(osc, diagnostics, config, app_state)
    finally:
        osc.close()


def test_auto_route_reference_signal_uses_explicit_card_channels(fake_x32, diagnostics, app_state):
    _patch_main_lr_outputs(fake_x32)
    for addr in addresses.ALL_USERROUT_OUT:
        fake_x32.extra_responses[addr] = (0,)

    config = AppConfig()
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        slots = auto_route_reference_signal(osc, diagnostics, config, app_state, card_channels=(10, 11))
    finally:
        osc.close()

    assert slots == (10, 11)
    assert config.echo_reference_card_channels == (10, 11)
    assert fake_x32.extra_responses[addresses.userrout_out_addr(10)] == (OUT15_TAP,)
    assert fake_x32.extra_responses[addresses.userrout_out_addr(11)] == (OUT16_TAP,)


def test_auto_route_reference_signal_explicit_channels_override_previous_choice(fake_x32, diagnostics, app_state):
    _patch_main_lr_outputs(fake_x32)
    for addr in addresses.ALL_USERROUT_OUT:
        fake_x32.extra_responses[addr] = (0,)

    config = AppConfig(echo_reference_card_channels=(1, 2))
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        slots = auto_route_reference_signal(osc, diagnostics, config, app_state, card_channels=(5, 6))
    finally:
        osc.close()

    assert slots == (5, 6)
    assert config.echo_reference_card_channels == (5, 6)


def test_auto_route_reference_signal_explicit_channels_reject_conflict(fake_x32, diagnostics, app_state):
    app_state.channels[9].card_out_slot = 5  # channel 9 already owns Card slot 5

    config = AppConfig()
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        with pytest.raises(EchoCancellationError, match="5"):
            auto_route_reference_signal(osc, diagnostics, config, app_state, card_channels=(5, 6))
    finally:
        osc.close()


def test_find_main_lr_outputs_reads_the_out_patch(fake_x32, diagnostics, app_state):
    _patch_main_lr_outputs(fake_x32, main_l_output=3, main_r_output=7)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        assert find_main_lr_outputs(osc) == (3, 7)
    finally:
        osc.close()


def test_find_main_lr_outputs_raises_when_nothing_patched_to_main(fake_x32, diagnostics, app_state):
    for n in range(1, addresses.NUM_MAIN_OUTPUTS + 1):
        fake_x32.extra_responses[addresses.output_src_addr(n)] = (4,)  # MixBus 01 everywhere
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        with pytest.raises(EchoCancellationError, match="Main L"):
            find_main_lr_outputs(osc)
    finally:
        osc.close()


def test_auto_route_uses_discovered_outputs_not_hardcoded_15_16(fake_x32, diagnostics, app_state):
    # A console where someone moved Main L/R to outputs 1/2 -- the tap
    # values written must follow the actual patch.
    _patch_main_lr_outputs(fake_x32, main_l_output=1, main_r_output=2)
    for addr in addresses.ALL_USERROUT_OUT:
        fake_x32.extra_responses[addr] = (0,)

    config = AppConfig()
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        slots = auto_route_reference_signal(osc, diagnostics, config, app_state)
    finally:
        osc.close()

    assert fake_x32.extra_responses[addresses.userrout_out_addr(slots[0])] == (addresses.output_userrout_out_value(1),)
    assert fake_x32.extra_responses[addresses.userrout_out_addr(slots[1])] == (addresses.output_userrout_out_value(2),)

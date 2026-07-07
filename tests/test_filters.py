from __future__ import annotations

import numpy as np
import pytest

from app.audio.filters import NotchBankFullError, NotchFilterBank

SAMPLE_RATE = 48000


def _tone(frequency_hz: float, duration_sec: float = 0.5, amplitude: float = 1.0) -> np.ndarray:
    t = np.arange(int(SAMPLE_RATE * duration_sec)) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * frequency_hz * t)).astype(np.float32)


def _tone_rms_db(signal: np.ndarray) -> float:
    rms = np.sqrt(np.mean(signal.astype(np.float64) ** 2)) + 1e-12
    return 20 * np.log10(rms)


def test_process_with_no_notches_is_passthrough():
    bank = NotchFilterBank(sample_rate=SAMPLE_RATE, max_notches=12, depth_db=-12.0)
    block = _tone(1000.0)
    output = bank.process(block)
    assert np.allclose(output, block)


def test_add_notch_attenuates_target_frequency():
    bank = NotchFilterBank(sample_rate=SAMPLE_RATE, max_notches=12, depth_db=-12.0, q=10.0)
    tone = _tone(1000.0, duration_sec=1.0)

    before_db = _tone_rms_db(tone)
    bank.add_notch(1000.0)
    after_db = _tone_rms_db(bank.process(tone))

    attenuation = before_db - after_db
    # Target depth is -12dB; allow tolerance for filter settling + Hann-free
    # tone measurement.
    assert 8.0 <= attenuation <= 16.0


def test_notch_does_not_significantly_attenuate_far_off_frequency():
    bank = NotchFilterBank(sample_rate=SAMPLE_RATE, max_notches=12, depth_db=-12.0, q=10.0)
    bank.add_notch(1000.0)
    tone = _tone(4000.0, duration_sec=1.0)

    before_db = _tone_rms_db(tone)
    after_db = _tone_rms_db(bank.process(tone))
    assert abs(before_db - after_db) < 1.0


def test_remove_notch_restores_passthrough():
    bank = NotchFilterBank(sample_rate=SAMPLE_RATE, max_notches=12, depth_db=-12.0)
    notch_id = bank.add_notch(1000.0)
    bank.remove_notch(notch_id)

    tone = _tone(1000.0, duration_sec=1.0)
    before_db = _tone_rms_db(tone)
    after_db = _tone_rms_db(bank.process(tone))
    assert abs(before_db - after_db) < 0.5


def test_max_notches_enforced():
    bank = NotchFilterBank(sample_rate=SAMPLE_RATE, max_notches=2, depth_db=-12.0)
    bank.add_notch(500.0)
    bank.add_notch(1000.0)
    with pytest.raises(NotchBankFullError):
        bank.add_notch(1500.0)


def test_active_notches_reports_current_set():
    bank = NotchFilterBank(sample_rate=SAMPLE_RATE, max_notches=12, depth_db=-12.0)
    notch_id = bank.add_notch(1000.0, q=8.0, depth_db=-6.0)
    active = bank.active_notches()
    assert active == [{"id": notch_id, "frequency_hz": 1000.0, "q": 8.0, "depth_db": -6.0}]


def test_process_preserves_state_across_calls():
    # Filter state (zi) must carry over between successive process() calls
    # -- otherwise every block would start from a cold, discontinuous state.
    bank = NotchFilterBank(sample_rate=SAMPLE_RATE, max_notches=12, depth_db=-12.0)
    bank.add_notch(1000.0)
    tone = _tone(1000.0, duration_sec=1.0)
    half = len(tone) // 2

    whole_output = NotchFilterBank(sample_rate=SAMPLE_RATE, max_notches=12, depth_db=-12.0)
    whole_output.add_notch(1000.0)
    combined = np.concatenate([whole_output.process(tone[:half]), whole_output.process(tone[half:])])

    single_shot = NotchFilterBank(sample_rate=SAMPLE_RATE, max_notches=12, depth_db=-12.0)
    single_shot.add_notch(1000.0)
    one_pass = single_shot.process(tone)

    assert np.allclose(combined, one_pass, atol=1e-6)

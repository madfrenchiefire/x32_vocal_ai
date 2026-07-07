from __future__ import annotations

import numpy as np

from app.audio.detection import FeedbackDetector

SAMPLE_RATE = 48000
FFT_SIZE = 4096


def _growing_tone(frequency_hz: float, num_frames: int, amplitude_start: float, amplitude_end: float) -> list:
    """A sequence of blocks at a fixed frequency with linearly ramping
    amplitude, to exercise sustained-growth detection."""
    amplitudes = np.linspace(amplitude_start, amplitude_end, num_frames)
    t = np.arange(FFT_SIZE) / SAMPLE_RATE
    return [(amp * np.sin(2 * np.pi * frequency_hz * t)).astype(np.float32) for amp in amplitudes]


def _harmonic_tone(fundamental_hz: float, num_frames: int, amplitude: float = 1.0) -> list:
    """A tone with strong 2nd/3rd/4th harmonics, held at constant (but
    sustained/growing) amplitude to isolate the harmonic-structure check."""
    amplitudes = np.linspace(amplitude * 0.5, amplitude, num_frames)
    t = np.arange(FFT_SIZE) / SAMPLE_RATE
    blocks = []
    for amp in amplitudes:
        signal = (
            np.sin(2 * np.pi * fundamental_hz * t)
            + 0.5 * np.sin(2 * np.pi * fundamental_hz * 2 * t)
            + 0.5 * np.sin(2 * np.pi * fundamental_hz * 3 * t)
            + 0.5 * np.sin(2 * np.pi * fundamental_hz * 4 * t)
        )
        blocks.append((amp * signal).astype(np.float32))
    return blocks


def test_sustained_growing_pure_tone_is_flagged():
    detector = FeedbackDetector(sample_rate=SAMPLE_RATE, fft_size=FFT_SIZE, sustained_growth_frames=3)
    blocks = _growing_tone(2000.0, num_frames=6, amplitude_start=0.05, amplitude_end=1.0)

    candidates = []
    for block in blocks:
        candidates = detector.analyze(block)

    assert candidates, "a sustained, growing, non-harmonic tone should be flagged"
    flagged_freqs = [c.frequency_hz for c in candidates]
    assert any(abs(f - 2000.0) < 50 for f in flagged_freqs)
    for c in candidates:
        assert c.sustained_growth is True
        assert c.harmonic_structure_present is False


def test_constant_amplitude_tone_is_not_flagged():
    detector = FeedbackDetector(sample_rate=SAMPLE_RATE, fft_size=FFT_SIZE, sustained_growth_frames=3)
    t = np.arange(FFT_SIZE) / SAMPLE_RATE
    block = (np.sin(2 * np.pi * 2000.0 * t)).astype(np.float32)

    candidates = []
    for _ in range(6):
        candidates = detector.analyze(block)

    assert candidates == []


def test_harmonic_rich_tone_is_not_flagged():
    detector = FeedbackDetector(sample_rate=SAMPLE_RATE, fft_size=FFT_SIZE, sustained_growth_frames=3)
    blocks = _harmonic_tone(500.0, num_frames=6)

    candidates = []
    for block in blocks:
        candidates = detector.analyze(block)

    assert candidates == []


def test_analyze_pads_short_blocks():
    detector = FeedbackDetector(sample_rate=SAMPLE_RATE, fft_size=FFT_SIZE)
    short_block = np.zeros(128, dtype=np.float32)
    # Should not raise despite being far shorter than fft_size.
    candidates = detector.analyze(short_block)
    assert candidates == []

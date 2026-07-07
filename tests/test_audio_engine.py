from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

import app.audio.engine as engine_module
from app.audio.detection import FeedbackCandidate, FeedbackDetector
from app.audio.engine import AudioEngine, AudioEngineError
from app.audio.filters import NotchFilterBank
from app.audio.ml.classifier import FeedbackClassifier
from app.config import AppConfig
from app.state import AppState


def test_feedback_classifier_is_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        FeedbackClassifier(onnx_model_path="model.onnx")


def _make_engine(diagnostics, **config_overrides) -> AudioEngine:
    config = AppConfig(audio_sample_rate=48000, audio_block_size=64, **config_overrides)
    filter_banks = {1: NotchFilterBank(sample_rate=48000, max_notches=12, depth_db=-12.0)}
    detector = MagicMock(spec=FeedbackDetector)
    detector.analyze.return_value = []
    return AudioEngine(config=config, diagnostics=diagnostics, filter_banks=filter_banks, detector=detector)


def test_start_without_configured_devices_raises(diagnostics):
    engine = _make_engine(diagnostics)
    with pytest.raises(AudioEngineError):
        engine.start()


def test_start_opens_stream_and_stop_closes_it(monkeypatch, diagnostics):
    fake_stream = MagicMock()
    stream_factory = MagicMock(return_value=fake_stream)
    monkeypatch.setattr(engine_module.sd, "Stream", stream_factory)

    engine = _make_engine(diagnostics, audio_input_device="Input A", audio_output_device="Output B")
    engine.start()

    fake_stream.start.assert_called_once()
    assert stream_factory.call_args.kwargs["device"] == ("Input A", "Output B")
    assert stream_factory.call_args.kwargs["samplerate"] == 48000
    assert stream_factory.call_args.kwargs["blocksize"] == 64
    assert stream_factory.call_args.kwargs["callback"] == engine._audio_callback

    engine.stop()
    fake_stream.stop.assert_called_once()
    fake_stream.close.assert_called_once()


def test_audio_callback_applies_notch_bank_per_channel(diagnostics):
    engine = _make_engine(diagnostics)
    engine.filter_banks[1].add_notch(1000.0)  # give channel 1's bank something to actually do
    tone = np.sin(2 * np.pi * 1000.0 * np.arange(64) / 48000).astype(np.float32)
    indata = np.stack([tone, tone], axis=1)  # channel 1 has a bank, channel 2 does not
    outdata = np.zeros_like(indata)

    engine._audio_callback(indata, outdata, 64, None, None)

    assert not np.allclose(outdata[:, 0], indata[:, 0])  # channel 1 was filtered (bank exists)
    np.testing.assert_allclose(outdata[:, 1], indata[:, 1])  # channel 2 passthrough (no bank)
    assert outdata[:, 0].dtype == indata.dtype


def test_audio_callback_pushes_copy_to_analysis_queue_without_blocking(diagnostics):
    engine = _make_engine(diagnostics)
    tone = np.zeros(64, dtype=np.float32)
    indata = np.stack([tone, tone], axis=1)
    outdata = np.zeros_like(indata)

    engine._audio_callback(indata, outdata, 64, None, None)

    queued = engine._analysis_queue.get_nowait()
    np.testing.assert_allclose(queued, indata)
    assert queued is not indata  # must be a copy -- callback owns indata's buffer


def test_analyze_block_adds_notch_for_confirmed_candidate(diagnostics):
    engine = _make_engine(diagnostics)
    candidate = FeedbackCandidate(
        frequency_hz=1000.0,
        peak_to_average_db=20.0,
        harmonic_structure_present=False,
        sustained_growth=True,
    )
    engine.detector.analyze.return_value = [candidate]

    block = np.zeros((64, 2), dtype=np.float32)
    engine._analyze_block(block)

    active = engine.filter_banks[1].active_notches()
    assert len(active) == 1
    assert active[0]["frequency_hz"] == 1000.0

    events = diagnostics.get_recent(5)
    notch_events = [e for e in events if e["category"] == "state_change" and e["payload"]["description"] == "notch_placed"]
    assert len(notch_events) == 1
    assert notch_events[0]["payload"]["after"]["frequency_hz"] == 1000.0


def test_analyze_block_skips_channel_without_bank(diagnostics):
    engine = _make_engine(diagnostics)
    candidate = FeedbackCandidate(
        frequency_hz=1000.0,
        peak_to_average_db=20.0,
        harmonic_structure_present=False,
        sustained_growth=True,
    )
    engine.detector.analyze.return_value = [candidate]

    block = np.zeros((64, 2), dtype=np.float32)
    engine._analyze_block(block)  # channel 2 (index 1) has no bank -- must not raise

    assert 2 not in engine.filter_banks


def test_analyze_block_respects_max_notches(diagnostics):
    engine = _make_engine(diagnostics)
    bank = engine.filter_banks[1]
    bank.max_notches = 1
    bank.add_notch(500.0)

    candidate = FeedbackCandidate(
        frequency_hz=1000.0,
        peak_to_average_db=20.0,
        harmonic_structure_present=False,
        sustained_growth=True,
    )
    engine.detector.analyze.return_value = [candidate]

    block = np.zeros((64, 2), dtype=np.float32)
    engine._analyze_block(block)  # bank already full -- must not raise NotchBankFullError

    active = engine.filter_banks[1].active_notches()
    assert len(active) == 1
    assert active[0]["frequency_hz"] == 500.0


def test_measure_round_trip_latency_requires_physical_hardware(diagnostics):
    engine = _make_engine(diagnostics)
    with pytest.raises(NotImplementedError):
        engine.measure_round_trip_latency()


def test_analyze_block_skips_channel_with_ai_disabled(diagnostics):
    engine = _make_engine(diagnostics)
    state = AppState()
    state.channels[1].ai_enabled = False
    engine.state = state

    candidate = FeedbackCandidate(
        frequency_hz=1000.0,
        peak_to_average_db=20.0,
        harmonic_structure_present=False,
        sustained_growth=True,
    )
    engine.detector.analyze.return_value = [candidate]

    block = np.zeros((64, 2), dtype=np.float32)
    engine._analyze_block(block)

    assert engine.filter_banks[1].active_notches() == []
    engine.detector.analyze.assert_not_called()


def test_analyze_block_processes_channel_with_ai_enabled(diagnostics):
    engine = _make_engine(diagnostics)
    state = AppState()
    state.channels[1].ai_enabled = True
    engine.state = state

    candidate = FeedbackCandidate(
        frequency_hz=1000.0,
        peak_to_average_db=20.0,
        harmonic_structure_present=False,
        sustained_growth=True,
    )
    engine.detector.analyze.return_value = [candidate]

    block = np.zeros((64, 2), dtype=np.float32)
    engine._analyze_block(block)

    active = engine.filter_banks[1].active_notches()
    assert len(active) == 1
    assert active[0]["frequency_hz"] == 1000.0

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import numpy as np
import pytest

import app.audio.engine as engine_module
from app.audio.detection import FeedbackCandidate, FeedbackDetector
from app.audio.devices import AudioDevice
from app.audio.echo_cancellation import EchoCanceller
from app.audio.engine import AudioEngine, AudioEngineError
from app.audio.filters import NotchFilterBank
from app.audio.ml.classifier import FeedbackClassifier
from app.config import AppConfig
from app.state import AppState


def _fake_asio_device(name: str, channels: int = 32) -> AudioDevice:
    return AudioDevice(
        index=0, name=name, host_api="ASIO", max_input_channels=channels, max_output_channels=channels,
        default_sample_rate=48000.0, is_asio=True,
    )


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


def _patch_devices(monkeypatch, input_device, output_device):
    def fake_find(name):
        return {input_device.name: input_device, output_device.name: output_device}.get(name)

    monkeypatch.setattr(engine_module, "find_device_by_name", fake_find)


def test_start_opens_stream_and_stop_closes_it(monkeypatch, diagnostics):
    fake_stream = MagicMock()
    stream_factory = MagicMock(return_value=fake_stream)
    monkeypatch.setattr(engine_module.sd, "Stream", stream_factory)
    _patch_devices(monkeypatch, _fake_asio_device("Input A"), _fake_asio_device("Output B"))

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


def test_start_raises_when_input_device_not_found(monkeypatch, diagnostics):
    _patch_devices(monkeypatch, _fake_asio_device("Input A"), _fake_asio_device("Output B"))
    engine = _make_engine(diagnostics, audio_input_device="Missing Device", audio_output_device="Output B")
    with pytest.raises(AudioEngineError, match="not found"):
        engine.start()


def test_start_raises_when_input_device_has_too_few_channels(monkeypatch, diagnostics):
    # filter_banks has key 1, so num_channels required is at least 1 -- use
    # a bank keyed at slot 5 to require 5 channels from a 2-channel device.
    engine = _make_engine(diagnostics, audio_input_device="Input A", audio_output_device="Output B")
    engine.filter_banks = {5: NotchFilterBank(sample_rate=48000, max_notches=12, depth_db=-12.0)}
    _patch_devices(monkeypatch, _fake_asio_device("Input A", channels=2), _fake_asio_device("Output B", channels=32))

    with pytest.raises(AudioEngineError, match="has only 2 input channel"):
        engine.start()


def test_start_raises_when_output_device_has_too_few_channels(monkeypatch, diagnostics):
    engine = _make_engine(diagnostics, audio_input_device="Input A", audio_output_device="Output B")
    engine.filter_banks = {5: NotchFilterBank(sample_rate=48000, max_notches=12, depth_db=-12.0)}
    _patch_devices(monkeypatch, _fake_asio_device("Input A", channels=32), _fake_asio_device("Output B", channels=2))

    with pytest.raises(AudioEngineError, match="has only 2 output channel"):
        engine.start()


def test_audio_callback_applies_notch_bank_per_channel(diagnostics):
    engine = _make_engine(diagnostics)
    engine.filter_banks[1].add_notch(1000.0)  # give channel 1's bank something to actually do
    tone = np.sin(2 * np.pi * 1000.0 * np.arange(64) / 48000).astype(np.float32)
    indata = np.stack([tone, tone], axis=1)  # channel 1 has a bank, channel 2 does not
    outdata = np.zeros_like(indata)

    engine._audio_callback(indata, outdata, 64, None, None)

    assert not np.allclose(outdata[:, 0], indata[:, 0])  # channel 1 was filtered (bank exists)
    # Channel 2 is unmanaged (no bank) -- its Card output is ignored by the
    # console in the insert design, so the callback zeroes it rather than
    # passing it through.
    np.testing.assert_allclose(outdata[:, 1], np.zeros(64, dtype=np.float32))
    assert outdata[:, 0].dtype == indata.dtype


def test_audio_callback_writes_processed_audio_to_aux_output_slot(diagnostics):
    # Insert design: a managed channel is read on its own input index but
    # its processed audio is written to a *different* output index -- the
    # channel's Aux/PC-output slot (card_out_slot), which feeds the insert
    # return. Here channel 1 is read on index 0 but written to slot 3.
    engine = _make_engine(diagnostics)
    state = AppState()
    state.channels[1].card_out_slot = 3
    engine.state = state
    engine.filter_banks[1].add_notch(1000.0)

    tone = np.sin(2 * np.pi * 1000.0 * np.arange(64) / 48000).astype(np.float32)
    indata = np.stack([tone] * 4, axis=1)
    outdata = np.zeros((64, 4), dtype=np.float32)

    engine._audio_callback(indata, outdata, 64, None, None)

    # Processed audio lands on output index 2 (slot 3), not the read index 0.
    assert not np.allclose(outdata[:, 2], np.zeros(64))
    np.testing.assert_allclose(outdata[:, 0], np.zeros(64, dtype=np.float32))


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
    state.channels[1].card_out_slot = 1
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
    state.channels[1].card_out_slot = 1
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


def test_analyze_block_gates_by_channel_number_directly(diagnostics):
    # Insert design: filter_banks are keyed by console channel number and
    # read on that channel's own input index, so gating is a direct
    # state.channels[channel] lookup with no card-slot reverse mapping. A
    # channel whose AI is off must be skipped even though it has a bank.
    from app.audio.filters import NotchFilterBank

    detector = MagicMock(spec=FeedbackDetector)
    candidate = FeedbackCandidate(
        frequency_hz=1000.0,
        peak_to_average_db=20.0,
        harmonic_structure_present=False,
        sustained_growth=True,
    )
    detector.analyze.return_value = [candidate]
    config = AppConfig(audio_sample_rate=48000, audio_block_size=64)
    engine = AudioEngine(
        config=config,
        diagnostics=diagnostics,
        filter_banks={
            1: NotchFilterBank(sample_rate=48000, max_notches=12, depth_db=-12.0),
            2: NotchFilterBank(sample_rate=48000, max_notches=12, depth_db=-12.0),
        },
        detector=detector,
    )
    state = AppState()
    state.channels[1].ai_enabled = True
    state.channels[1].sensitivity = 1.0
    state.channels[2].ai_enabled = False  # has a bank, but AI off -> skipped
    engine.state = state

    engine._analyze_block(np.zeros((64, 2), dtype=np.float32))

    assert detector.analyze.call_count == 1
    assert detector.analyze.call_args.kwargs["channel_key"] == 1
    assert detector.analyze.call_args.kwargs["threshold_db"] == pytest.approx(4.0)
    events = [e for e in diagnostics.get_recent(10) if e["payload"].get("description") == "notch_placed"]
    assert len(events) == 1
    assert events[0]["payload"]["after"]["channel"] == 1
    assert engine.filter_banks[2].active_notches() == []  # channel 2 gated closed


def test_analyze_block_skips_slot_with_no_channel_assigned(diagnostics):
    # State is wired up, but no channel has claimed Card slot 1 -- must not
    # default-open the gate (and must not touch the mock detector at all).
    engine = _make_engine(diagnostics)
    engine.state = AppState()

    block = np.zeros((64, 2), dtype=np.float32)
    engine._analyze_block(block)

    engine.detector.analyze.assert_not_called()
    assert engine.filter_banks[1].active_notches() == []


def test_analyze_block_passes_sensitivity_derived_threshold(diagnostics):
    engine = _make_engine(diagnostics)
    state = AppState()
    state.channels[1].card_out_slot = 1
    state.channels[1].ai_enabled = True
    state.channels[1].sensitivity = 1.0  # maximally sensitive -> lowest threshold
    engine.state = state

    block = np.zeros((64, 2), dtype=np.float32)
    engine._analyze_block(block)

    engine.detector.analyze.assert_called_once()
    kwargs = engine.detector.analyze.call_args.kwargs
    assert kwargs["channel_key"] == 1
    assert kwargs["threshold_db"] == pytest.approx(4.0)  # MIN_THRESHOLD_DB at sensitivity=1.0


def test_analyze_block_ring_out_mode_lowers_threshold_further(diagnostics):
    engine = _make_engine(diagnostics)
    state = AppState()
    state.channels[1].card_out_slot = 1
    state.channels[1].ai_enabled = True
    state.channels[1].sensitivity = 0.5
    state.channels[1].mode = "ring_out"
    engine.state = state

    block = np.zeros((64, 2), dtype=np.float32)
    engine._analyze_block(block)

    threshold_db = engine.detector.analyze.call_args.kwargs["threshold_db"]
    assert threshold_db == pytest.approx(12.0 - engine_module.RING_OUT_THRESHOLD_ADJUSTMENT_DB)


def test_analyze_block_reconfirms_existing_notch_instead_of_duplicating(diagnostics):
    engine = _make_engine(diagnostics)
    bank = engine.filter_banks[1]
    notch_id = bank.add_notch(1000.0)

    candidate = FeedbackCandidate(
        frequency_hz=1005.0,  # within NOTCH_MATCH_TOLERANCE_HZ of the existing notch
        peak_to_average_db=20.0,
        harmonic_structure_present=False,
        sustained_growth=True,
    )
    engine.detector.analyze.return_value = [candidate]

    block = np.zeros((64, 2), dtype=np.float32)
    engine._analyze_block(block)

    active = bank.active_notches()
    assert len(active) == 1
    assert active[0]["id"] == notch_id  # no duplicate notch created
    events = [e for e in diagnostics.get_recent(10) if e["payload"].get("description") == "notch_placed"]
    assert events == []  # reconfirmed, not (re)placed


def test_analyze_block_live_mode_releases_stale_notches(diagnostics):
    engine = _make_engine(diagnostics)
    state = AppState()
    state.channels[1].card_out_slot = 1
    state.channels[1].ai_enabled = True
    state.channels[1].mode = "live"
    engine.state = state

    bank = engine.filter_banks[1]
    stale_id = bank.add_notch(500.0)
    bank.touch_notch(stale_id, now=0.0)  # ancient reconfirmation

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(engine_module.time, "monotonic", lambda: 10_000.0)
        block = np.zeros((64, 2), dtype=np.float32)
        engine._analyze_block(block)

    assert bank.active_notches() == []
    released_events = [e for e in diagnostics.get_recent(10) if e["payload"].get("description") == "notch_released"]
    assert len(released_events) == 1
    assert released_events[0]["payload"]["after"]["notch_id"] == stale_id


def test_analyze_block_ring_out_mode_never_releases_notches(diagnostics):
    engine = _make_engine(diagnostics)
    state = AppState()
    state.channels[1].card_out_slot = 1
    state.channels[1].ai_enabled = True
    state.channels[1].mode = "ring_out"
    engine.state = state

    bank = engine.filter_banks[1]
    notch_id = bank.add_notch(500.0)
    bank.touch_notch(notch_id, now=0.0)  # ancient reconfirmation -- would be released in live mode

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(engine_module.time, "monotonic", lambda: 10_000.0)
        block = np.zeros((64, 2), dtype=np.float32)
        engine._analyze_block(block)

    assert [n["id"] for n in bank.active_notches()] == [notch_id]


def _engine_with_echo_canceller(diagnostics, config_overrides=None) -> AudioEngine:
    config = AppConfig(
        audio_sample_rate=48000, audio_block_size=64,
        echo_reference_card_channels=(3, 4), **(config_overrides or {}),
    )
    filter_banks = {1: NotchFilterBank(sample_rate=48000, max_notches=12, depth_db=-12.0)}
    echo_cancellers = {1: EchoCanceller(filter_length_taps=16)}
    return AudioEngine(
        config=config, diagnostics=diagnostics, filter_banks=filter_banks,
        detector=MagicMock(spec=FeedbackDetector), echo_cancellers=echo_cancellers,
    )


def _reference_carrying_indata(num_channels: int = 4) -> np.ndarray:
    # Card slots 3/4 (config.echo_reference_card_channels) carry a tone so
    # _read_reference_block returns something non-None; channel 1 carries
    # a different, correlated-enough signal to exercise process().
    t = np.arange(64) / 48000
    tone = np.sin(2 * np.pi * 1000.0 * t).astype(np.float32)
    indata = np.zeros((64, num_channels), dtype=np.float32)
    indata[:, 0] = tone  # slot 1
    indata[:, 2] = tone  # slot 3 (reference L)
    indata[:, 3] = tone  # slot 4 (reference R)
    return indata


def test_audio_callback_applies_echo_cancellation_with_no_state_wired(diagnostics):
    # No AppState -- matches this class's "None = no gating" convention.
    engine = _engine_with_echo_canceller(diagnostics)
    canceller = engine.echo_cancellers[1]
    canceller.process = MagicMock(side_effect=lambda signal, ref: signal)

    indata = _reference_carrying_indata()
    outdata = np.zeros_like(indata)
    engine._audio_callback(indata, outdata, 64, None, None)

    canceller.process.assert_called_once()


def test_audio_callback_skips_echo_cancellation_when_channel_toggle_off(diagnostics):
    engine = _engine_with_echo_canceller(diagnostics)
    state = AppState()
    state.channels[1].card_out_slot = 1
    state.channels[1].echo_cancellation_enabled = False  # the default -- previously dead
    engine.state = state
    canceller = engine.echo_cancellers[1]
    canceller.process = MagicMock(side_effect=lambda signal, ref: signal)

    indata = _reference_carrying_indata()
    outdata = np.zeros_like(indata)
    engine._audio_callback(indata, outdata, 64, None, None)

    canceller.process.assert_not_called()


def test_audio_callback_applies_echo_cancellation_when_channel_toggle_on(diagnostics):
    engine = _engine_with_echo_canceller(diagnostics)
    state = AppState()
    state.channels[1].card_out_slot = 1
    state.channels[1].echo_cancellation_enabled = True
    engine.state = state
    canceller = engine.echo_cancellers[1]
    canceller.process = MagicMock(side_effect=lambda signal, ref: signal)

    indata = _reference_carrying_indata()
    outdata = np.zeros_like(indata)
    engine._audio_callback(indata, outdata, 64, None, None)

    canceller.process.assert_called_once()


def test_audio_callback_echo_toggle_gated_by_same_channel_state(diagnostics):
    # Echo cancellers are keyed by console channel number too, so the toggle
    # is read from that channel's own ChannelState -- no card-slot mapping.
    engine = _engine_with_echo_canceller(diagnostics)
    state = AppState()
    state.channels[1].echo_cancellation_enabled = True
    state.channels[2].echo_cancellation_enabled = False
    engine.state = state
    canceller = engine.echo_cancellers[1]
    canceller.process = MagicMock(side_effect=lambda signal, ref: signal)

    indata = _reference_carrying_indata()
    outdata = np.zeros_like(indata)
    engine._audio_callback(indata, outdata, 64, None, None)

    canceller.process.assert_called_once()


def test_audio_callback_updates_levels(diagnostics):
    engine = _make_engine(diagnostics)
    tone = np.ones(64, dtype=np.float32)  # RMS 1.0 -> 0dBFS
    silence = np.zeros(64, dtype=np.float32)
    indata = np.stack([tone, silence], axis=1)
    outdata = np.zeros_like(indata)

    engine._audio_callback(indata, outdata, 64, None, None)

    levels = engine.get_levels()
    assert levels[1] == pytest.approx(0.0, abs=0.1)
    assert levels[2] == engine_module.LEVEL_FLOOR_DB


def test_meters_loop_broadcasts_via_hook(diagnostics, monkeypatch):
    monkeypatch.setattr(engine_module, "METERS_BROADCAST_INTERVAL_SEC", 0.02)
    updates = []
    engine = _make_engine(diagnostics)
    engine.on_levels_update = lambda levels: updates.append(levels)
    engine._levels[1] = -6.0
    engine._running.set()

    thread = threading.Thread(target=engine._meters_loop, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 2.0
        while not updates and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        engine._running.clear()
        thread.join(timeout=1.0)

    assert updates
    assert updates[0] == {1: -6.0}


def test_analyze_block_calls_saturation_hook_when_bank_full(diagnostics):
    engine = _make_engine(diagnostics)
    state = AppState()
    state.channels[1].card_out_slot = 1
    state.channels[1].ai_enabled = True
    engine.state = state

    bank = engine.filter_banks[1]
    bank.max_notches = 1
    bank.add_notch(500.0)  # bank now full

    saturated: list[int] = []
    engine.on_notch_bank_saturated = saturated.append

    candidate = FeedbackCandidate(
        frequency_hz=2000.0,  # far from the existing 500 Hz notch
        peak_to_average_db=20.0,
        harmonic_structure_present=False,
        sustained_growth=True,
    )
    engine.detector.analyze.return_value = [candidate]

    block = np.zeros((64, 2), dtype=np.float32)
    engine._analyze_block(block)

    assert saturated == [1]  # console channel number
    assert len(bank.active_notches()) == 1  # no notch stacked past the cap


def test_analyze_block_no_saturation_hook_when_room_left(diagnostics):
    engine = _make_engine(diagnostics)
    state = AppState()
    state.channels[1].card_out_slot = 1
    state.channels[1].ai_enabled = True
    engine.state = state

    saturated: list[int] = []
    engine.on_notch_bank_saturated = saturated.append

    candidate = FeedbackCandidate(
        frequency_hz=2000.0,
        peak_to_average_db=20.0,
        harmonic_structure_present=False,
        sustained_growth=True,
    )
    engine.detector.analyze.return_value = [candidate]
    engine._analyze_block(np.zeros((64, 2), dtype=np.float32))

    assert saturated == []

"""Round-trip latency measurement -- no loopback cable required.

CLAUDE.md Phase 1 calls for a loopback click test ("target total round
trip <= ~10 ms; measure it"). The original plan needed a physical cable
from an output jack to an input jack; the X32's routing makes that
unnecessary: a User Out slot can tap **"Card In N"** (userrout/out values
129-160, doc-confirmed in X32_OSC.pdf) -- the signal arriving at the
console FROM the card, i.e. whatever this app is playing out on card
channel N. Pointing Card return M at that tap loops the app's own output
straight back to its own input entirely inside the console:

    app plays click on card channel N
      -> console sees it as "Card In N"
      -> /config/userrout/out/M = 129 + (N-1)   (User Out M taps it)
      -> CARD block containing M set to the matching User Out bank
      -> card return M carries the click back to the app

The measured figure is the full USB round trip (app -> driver -> card ->
console routing -> card -> driver -> app) -- everything except the
analog converter stages, which never engage on this purely digital loop.
Real-world mic-to-PA latency adds roughly one AD + one DA pass (~1 ms
total) on top of what this reports.

Console routing is snapshotted before and restored after, per "snapshot
before touching anything" -- including on failure.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from app.config import AppConfig
from app.diagnostics.logger import DiagnosticsLogger
from app.osc import addresses
from app.osc.connection import OscConnection

CLICK_LENGTH_SAMPLES = 64
PRE_ROLL_SEC = 0.25  # silence before the click, so delay 0 is detectable
RECORD_TAIL_SEC = 0.75  # how long past the click to keep listening
# Correlation peak must dominate the recording's overall energy by this
# factor, or we declare "no click seen" instead of returning noise.
DETECTION_PEAK_RATIO = 8.0


class LatencyMeasurementError(Exception):
    pass


@dataclass
class LatencyResult:
    round_trip_samples: int
    round_trip_ms: float
    sample_rate: int
    out_card_slot: int
    in_card_slot: int
    # This loop never goes analog -- AD/DA converter latency (~1 ms
    # combined) is NOT included in the figure above.
    includes_converters: bool = False


def generate_click(sample_rate: int) -> tuple[np.ndarray, int]:
    """(playback_mono, click_start_sample): pre-roll silence, then a short
    broadband click (fixed-seed noise burst -- broadband correlates far
    more sharply than a tone), then tail silence."""
    rng = np.random.default_rng(1234)
    click = rng.uniform(-1.0, 1.0, CLICK_LENGTH_SAMPLES).astype(np.float32) * 0.9
    pre_roll = int(PRE_ROLL_SEC * sample_rate)
    tail = int(RECORD_TAIL_SEC * sample_rate)
    playback = np.zeros(pre_roll + CLICK_LENGTH_SAMPLES + tail, dtype=np.float32)
    playback[pre_roll:pre_roll + CLICK_LENGTH_SAMPLES] = click
    return playback, pre_roll


def find_click_delay_samples(recording: np.ndarray, playback: np.ndarray, click_start: int) -> int:
    """Cross-correlate the recording against the known click template and
    return how many samples later than the playback position it arrived.
    Raises LatencyMeasurementError if no convincing click is present
    (silent/berserk recording), rather than returning a noise peak."""
    template = playback[click_start:click_start + CLICK_LENGTH_SAMPLES]
    recording = recording.astype(np.float64).ravel()
    correlation = np.correlate(recording, template.astype(np.float64), mode="valid")
    peak_index = int(np.argmax(np.abs(correlation)))
    peak = abs(correlation[peak_index])

    mean_abs = float(np.mean(np.abs(correlation))) + 1e-12
    if peak / mean_abs < DETECTION_PEAK_RATIO:
        raise LatencyMeasurementError(
            "no click detected in the loopback recording -- check that the console loopback "
            "routing applied and the audio device channels are correct"
        )
    delay = peak_index - click_start
    if delay < 0:
        raise LatencyMeasurementError(
            f"click appears {-delay} samples BEFORE it was played -- channel mapping is wrong"
        )
    return delay


def _default_run_playrec(config: AppConfig, playback_multi: np.ndarray, in_channels: int) -> np.ndarray:
    """Play playback_multi (frames x out_channels) and record in_channels
    simultaneously on the configured devices. Imported lazily so the rest
    of this module (and its tests) work without PortAudio installed.

    `blocksize`/`latency` are passed explicitly: without them PortAudio (and
    the ASIO driver behind it) falls back to its own default buffer, which
    ignores whatever you set in the ASIO control panel and makes the
    measured latency reflect that default rather than `audio_block_size`.
    Requesting the size directly is what actually pins the ASIO buffer to
    it (e.g. 64) for this measurement."""
    import sounddevice as sd

    recording = sd.playrec(
        playback_multi,
        samplerate=config.audio_sample_rate,
        channels=in_channels,
        input_mapping=None,
        device=(config.audio_input_device, config.audio_output_device),
        blocksize=config.audio_block_size,
        latency="low",
        blocking=True,
    )
    return recording


def measure_round_trip_latency(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    config: AppConfig,
    out_card_slot: int = 32,
    in_card_slot: int = 32,
    correlation_id: str | None = None,
    pace_sec: float = 0.02,
    run_playrec: Callable[[AppConfig, np.ndarray, int], np.ndarray] | None = None,
) -> LatencyResult:
    """Set up the console-internal loopback, play a click out card channel
    out_card_slot, detect it on card return in_card_slot, restore the
    console routing, and return the measured round trip.

    run_playrec is the hardware boundary (defaults to a sounddevice
    playrec on the configured ASIO devices); injectable for tests."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    run_playrec = run_playrec or _default_run_playrec

    userrout_addr = addresses.userrout_out_addr(in_card_slot)
    block_addr = addresses.card_block_addr_for_slot(in_card_slot)
    loop_value = addresses.card_in_userrout_out_value(out_card_slot)
    block_value = addresses.user_out_card_block_value(in_card_slot)

    # Snapshot the two console values this measurement touches.
    original = osc.query_many([userrout_addr, block_addr], correlation_id=correlation_id)
    if original[userrout_addr] is None or original[block_addr] is None:
        raise LatencyMeasurementError(
            f"could not read current values of {userrout_addr} / {block_addr} to snapshot them -- refusing to write"
        )

    diagnostics.log_state_change(
        "latency_loopback_routed",
        before={userrout_addr: original[userrout_addr][0], block_addr: original[block_addr][0]},
        after={userrout_addr: loop_value, block_addr: block_value},
        correlation_id=correlation_id,
    )

    try:
        for address, value in ((userrout_addr, loop_value), (block_addr, block_value)):
            osc.send(address, value, correlation_id=correlation_id)
            time.sleep(pace_sec)
            actual = osc.query_until_match(address, value, correlation_id=correlation_id)
            if actual != value:
                raise LatencyMeasurementError(f"loopback routing write failed: {address} read back {actual!r}")

        playback, click_start = generate_click(config.audio_sample_rate)
        out_channels = max(out_card_slot, 1)
        playback_multi = np.zeros((len(playback), out_channels), dtype=np.float32)
        playback_multi[:, out_card_slot - 1] = playback

        recording = run_playrec(config, playback_multi, in_card_slot)
        recorded_channel = recording[:, in_card_slot - 1] if recording.ndim == 2 else recording

        delay_samples = find_click_delay_samples(recorded_channel, playback, click_start)
    finally:
        # Restore the console exactly as found, success or failure.
        for address in (userrout_addr, block_addr):
            osc.send(address, original[address][0], correlation_id=correlation_id)
            time.sleep(pace_sec)
        diagnostics.log_state_change(
            "latency_loopback_restored",
            after={userrout_addr: original[userrout_addr][0], block_addr: original[block_addr][0]},
            correlation_id=correlation_id,
        )

    result = LatencyResult(
        round_trip_samples=delay_samples,
        round_trip_ms=1000.0 * delay_samples / config.audio_sample_rate,
        sample_rate=config.audio_sample_rate,
        out_card_slot=out_card_slot,
        in_card_slot=in_card_slot,
    )
    diagnostics.log_state_change(
        "latency_measured",
        after={
            "round_trip_samples": result.round_trip_samples,
            "round_trip_ms": round(result.round_trip_ms, 3),
            "includes_converters": result.includes_converters,
        },
        correlation_id=correlation_id,
    )
    return result

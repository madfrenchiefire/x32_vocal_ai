"""Real-time audio engine.

sounddevice (PortAudio) against the configured input/output devices
(AppConfig.audio_input_device/audio_output_device -- see app.audio.devices).
The audio callback does ONLY per-channel notch filtering
(app.audio.filters.NotchFilterBank) plus, if enabled, echo cancellation
(app.audio.echo_cancellation.EchoCanceller) -- both classical DSP, no FFT
or ML in the callback. FFT analysis (app.audio.detection.FeedbackDetector)
runs on a separate thread fed by a bounded queue from the callback.

Honest caveat on "no allocation in the callback": the notch/echo DSP
itself is allocation-free (preallocated SOS/filter state), but handing a
copy of each block to the analysis thread via queue.put_nowait(block.copy())
does allocate. A fully lock-free preallocated ring buffer would remove
that, but a plain Python callback via sounddevice is soft-real-time at
best regardless (GIL, interpreter overhead) -- this is flagged as a known,
minor deviation rather than quietly claimed away.
"""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable

import numpy as np

try:
    import sounddevice as sd
    _IMPORT_ERROR: OSError | None = None
except OSError as exc:
    sd = None
    _IMPORT_ERROR = exc

from app.audio.detection import FeedbackDetector, sensitivity_to_threshold_db
from app.audio.devices import find_device_by_name
from app.audio.echo_cancellation import EchoCanceller
from app.audio.filters import NotchFilterBank
from app.config import AppConfig
from app.diagnostics.logger import DiagnosticsLogger
from app.state import AppState, ChannelState

ANALYSIS_QUEUE_SIZE = 64
ANALYSIS_POLL_TIMEOUT_SEC = 0.5

# Notch aging: how "reconfirmed" (still-active-feedback) tracking gates
# live-mode's "slow release of unused notches" vs ring-out mode "locking"
# filters for the whole session (CLAUDE.md's Web UI modes).
NOTCH_RELEASE_AFTER_SEC = 120.0
NOTCH_MATCH_TOLERANCE_HZ = 20.0  # ~1.5x the default detector's FFT bin width
RING_OUT_THRESHOLD_ADJUSTMENT_DB = 4.0  # ring-out is more trigger-happy than the raw sensitivity mapping

METERS_BROADCAST_INTERVAL_SEC = 0.15
LEVEL_FLOOR_DB = -60.0


def _rms_dbfs(signal: np.ndarray) -> float:
    """RMS level in dBFS, floored at LEVEL_FLOOR_DB. The float64 cast here
    is a small per-block-per-channel allocation on the same honest "soft
    real-time regardless" basis as the analysis queue's block.copy() below
    -- meters are cosmetic display, not part of the DSP signal path."""
    rms = float(np.sqrt(np.mean(signal.astype(np.float64) ** 2)))
    return LEVEL_FLOOR_DB if rms <= 0.0 else max(LEVEL_FLOOR_DB, 20.0 * np.log10(rms))


class AudioEngineError(Exception):
    pass


class AudioEngine:
    def __init__(
        self,
        config: AppConfig,
        diagnostics: DiagnosticsLogger,
        filter_banks: dict[int, NotchFilterBank] | None = None,
        detector: FeedbackDetector | None = None,
        echo_cancellers: dict[int, EchoCanceller] | None = None,
        state: AppState | None = None,
        on_levels_update: Callable[[dict[int, float]], None] | None = None,
        on_notch_bank_saturated: Callable[[int], None] | None = None,
    ) -> None:
        self.config = config
        self.diagnostics = diagnostics
        self.filter_banks = filter_banks if filter_banks is not None else {}
        self.detector = detector or FeedbackDetector(sample_rate=config.audio_sample_rate)
        self.echo_cancellers = echo_cancellers if echo_cancellers is not None else {}
        # Gates notch placement by ChannelState.ai_enabled (the MIDI/web
        # "AI on/off" toggle) when provided. None (the default, used by
        # standalone/unit-test callers with no AppState wired up) means
        # "no gating" -- always analyze, matching this class's behavior
        # before the toggle existed.
        self.state = state
        # Injectable hook (same pattern as MidiService's on_ai_toggle etc.)
        # -- called from a dedicated low-rate thread, never the audio
        # callback itself, with {card_slot: level_dbfs} every
        # METERS_BROADCAST_INTERVAL_SEC. app.web.sockets wires this to a
        # WebSocket broadcast.
        self.on_levels_update = on_levels_update
        # Called (from the analysis thread; must not block) when a
        # channel's notch bank is full and detection still fires --
        # wired to app.osc.gain_assist.GainAssist.request_trim.
        self.on_notch_bank_saturated = on_notch_bank_saturated

        self._stream = None
        self._analysis_queue: queue.Queue = queue.Queue(maxsize=ANALYSIS_QUEUE_SIZE)
        self._analysis_thread: threading.Thread | None = None
        self._meters_thread: threading.Thread | None = None
        self._running = threading.Event()
        self._levels: dict[int, float] = {}

    def start(self) -> None:
        if sd is None:
            raise AudioEngineError("PortAudio is not available on this system") from _IMPORT_ERROR
        if self.config.audio_input_device is None or self.config.audio_output_device is None:
            raise AudioEngineError(
                "audio_input_device/audio_output_device not configured -- "
                "see app.audio.devices.list_input_devices()/list_output_devices()"
            )

        num_channels = max(
            [*self.filter_banks.keys(), *(self.config.echo_reference_card_channels or ())],
            default=1,
        )
        self._validate_device_channel_counts(num_channels)

        self._running.set()
        self._analysis_thread = threading.Thread(target=self._analysis_loop, name="audio-analysis", daemon=True)
        self._analysis_thread.start()
        self._meters_thread = threading.Thread(target=self._meters_loop, name="audio-meters", daemon=True)
        self._meters_thread.start()

        self._stream = sd.Stream(
            device=(self.config.audio_input_device, self.config.audio_output_device),
            samplerate=self.config.audio_sample_rate,
            blocksize=self.config.audio_block_size,
            channels=num_channels,
            dtype="float32",
            callback=self._audio_callback,
        )
        self._stream.start()
        self.diagnostics.log_state_change(
            "audio_engine_started",
            after={"input": self.config.audio_input_device, "output": self.config.audio_output_device},
        )

    def _validate_device_channel_counts(self, num_channels: int) -> None:
        """Card slot N is assumed to be channel index N-1 of the opened
        stream (app.audio.devices' module docstring) -- that only holds if
        the device actually has that many channels, so check explicitly
        rather than let PortAudio fail with a less legible error, or worse,
        silently open fewer channels than the routing/echo-reference setup
        expects."""
        input_device = find_device_by_name(self.config.audio_input_device)
        if input_device is None:
            raise AudioEngineError(f"audio_input_device {self.config.audio_input_device!r} not found")
        if input_device.max_input_channels < num_channels:
            raise AudioEngineError(
                f"audio_input_device {self.config.audio_input_device!r} has only "
                f"{input_device.max_input_channels} input channel(s), but {num_channels} are needed "
                "(one per provisioned Card slot / echo reference channel)"
            )

        output_device = find_device_by_name(self.config.audio_output_device)
        if output_device is None:
            raise AudioEngineError(f"audio_output_device {self.config.audio_output_device!r} not found")
        if output_device.max_output_channels < num_channels:
            raise AudioEngineError(
                f"audio_output_device {self.config.audio_output_device!r} has only "
                f"{output_device.max_output_channels} output channel(s), but {num_channels} are needed "
                "(one per provisioned Card slot / echo reference channel)"
            )

    def stop(self) -> None:
        self._running.clear()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._analysis_thread is not None:
            self._analysis_thread.join(timeout=ANALYSIS_POLL_TIMEOUT_SEC + 1)
            self._analysis_thread = None
        if self._meters_thread is not None:
            self._meters_thread.join(timeout=METERS_BROADCAST_INTERVAL_SEC + 1)
            self._meters_thread = None
        self.diagnostics.log_state_change("audio_engine_stopped")

    def get_levels(self) -> dict[int, float]:
        """Current per-Card-slot RMS level snapshot, in dBFS. Synchronous
        fallback for callers without a WebSocket connection (e.g. a fresh
        page load) -- on_levels_update is the live-push path."""
        return dict(self._levels)

    def measure_round_trip_latency(self) -> float:
        """Implemented in app.audio.latency (no loopback cable needed --
        the console's own routing loops the app's output back digitally);
        run `python -m app.tools.measure_latency --console <ip>` on the
        target PC. Not a method here because the measurement needs an OSC
        connection and exclusive use of the audio device, neither of which
        the running engine has to give."""
        raise NotImplementedError(
            "use `python -m app.tools.measure_latency --console <ip>` (app.audio.latency) on the target PC"
        )

    def _audio_callback(self, indata: np.ndarray, outdata: np.ndarray, frames: int, time_info, status) -> None:
        """PortAudio callback. Real-time constraint: filter processing
        only, no analysis/ML, no diagnostics logging on the hot path."""
        reference_block = self._read_reference_block(indata)
        for channel_index in range(indata.shape[1]):
            channel_number = channel_index + 1
            raw_signal = indata[:, channel_index]
            # Raw input level, not the filtered/processed output -- this is
            # "is signal actually arriving on this Card slot," independent
            # of whatever DSP happens to it afterward. Just a dict write,
            # no I/O -- the separate _meters_loop thread does the (slower,
            # unbounded-latency-tolerant) broadcasting.
            self._levels[channel_number] = _rms_dbfs(raw_signal)
            signal = raw_signal

            canceller = self.echo_cancellers.get(channel_number)
            if canceller is not None and reference_block is not None and self._echo_cancellation_wanted(channel_number):
                signal = canceller.process(signal, reference_block)

            bank = self.filter_banks.get(channel_number)
            outdata[:, channel_index] = bank.process(signal) if bank is not None else signal

        try:
            self._analysis_queue.put_nowait(indata.copy())
        except queue.Full:
            pass  # analysis thread is behind -- drop this block rather than block the callback

    def _channel_state_for_slot(self, card_slot: int) -> ChannelState | None:
        """Card slot N is NOT necessarily console channel N -- apply_routing
        can assign any channel to any free slot (app.osc.routing_apply
        reuses whichever Card slot is free, not one matching the channel's
        own number). Finds which channel, if any, currently owns this slot,
        so per-channel settings (ai_enabled, sensitivity, mode,
        echo_cancellation_enabled) gate the *right* channel's audio instead
        of accidentally reading slot N's own ChannelState.channels[N] --
        those are two different things whenever a channel lands on a
        non-matching slot, which was a real, previously-shipped bug this
        fixes. O(32) linear scan, negligible next to the FFT/filtering work
        already happening per block."""
        if self.state is None:
            return None
        for channel_state in self.state.channels.values():
            if channel_state.card_out_slot == card_slot:
                return channel_state
        return None

    def _echo_cancellation_wanted(self, channel_number: int) -> bool:
        """Gates the per-slot EchoCanceller by ChannelState.
        echo_cancellation_enabled -- previously a dead field: cancellers
        were provisioned for every slot the moment the *global*
        AppConfig.echo_cancellation_enabled toggle was on, so this
        per-channel opt-in never actually did anything. None (no AppState
        wired up, e.g. standalone/test callers) means "no gating," matching
        this class's ai_enabled/None convention elsewhere; state wired up
        but no channel currently assigned to this slot means "nothing to
        gate by," so it stays off rather than defaulting on."""
        if self.state is None:
            return True
        channel_state = self._channel_state_for_slot(channel_number)
        return channel_state is not None and channel_state.echo_cancellation_enabled

    def _read_reference_block(self, indata: np.ndarray) -> np.ndarray | None:
        """Mono-mixed echo reference from the two Card channels the console's
        Main L/R bus was auto-routed onto (app.audio.echo_cancellation.
        auto_route_reference_signal). None if echo cancellation hasn't been
        set up, or the stream wasn't opened wide enough to include those
        channels."""
        ref_channels = self.config.echo_reference_card_channels
        if ref_channels is None:
            return None
        slot_a, slot_b = ref_channels
        if slot_a > indata.shape[1] or slot_b > indata.shape[1]:
            return None
        return ((indata[:, slot_a - 1] + indata[:, slot_b - 1]) * 0.5).astype(indata.dtype, copy=False)

    def _analysis_loop(self) -> None:
        while self._running.is_set():
            try:
                block = self._analysis_queue.get(timeout=ANALYSIS_POLL_TIMEOUT_SEC)
            except queue.Empty:
                continue
            self._analyze_block(block)

    def _meters_loop(self) -> None:
        while self._running.is_set():
            time.sleep(METERS_BROADCAST_INTERVAL_SEC)
            if self.on_levels_update is not None:
                self.on_levels_update(dict(self._levels))

    def _analyze_block(self, block: np.ndarray) -> None:
        now = time.monotonic()
        for channel_index in range(block.shape[1]):
            card_slot = channel_index + 1
            bank = self.filter_banks.get(card_slot)
            if bank is None:
                continue

            channel_state = None
            if self.state is not None:
                channel_state = self._channel_state_for_slot(card_slot)
                if channel_state is None or not channel_state.ai_enabled:
                    continue

            # Reported/logged channel identity: the console channel number
            # (channel_state.index) when known, falling back to the raw
            # Card slot for standalone/test callers with no AppState.
            reported_channel = channel_state.index if channel_state is not None else card_slot

            threshold_db = None
            if channel_state is not None:
                threshold_db = sensitivity_to_threshold_db(channel_state.sensitivity)
                if channel_state.mode == "ring_out":
                    # Ring-out/setup: deliberately more trigger-happy than
                    # the raw sensitivity mapping (CLAUDE.md's "aggressive").
                    threshold_db -= RING_OUT_THRESHOLD_ADJUSTMENT_DB

            candidates = self.detector.analyze(
                block[:, channel_index], channel_key=card_slot, threshold_db=threshold_db
            )
            for candidate in candidates:
                existing_id = bank.find_notch_near(candidate.frequency_hz, NOTCH_MATCH_TOLERANCE_HZ)
                if existing_id is not None:
                    # Same tone an existing notch is already suppressing --
                    # reconfirm it instead of stacking a redundant notch.
                    bank.touch_notch(existing_id, now=now)
                    continue
                if len(bank.active_notches()) >= bank.max_notches:
                    # Bank saturated AND detection still firing: notching
                    # has lost the gain-before-feedback battle on this
                    # channel. The hook (app.osc.gain_assist, opt-in) may
                    # trim the preamp -- it only enqueues, so calling it
                    # from this analysis thread is safe.
                    if self.on_notch_bank_saturated is not None and channel_state is not None:
                        self.on_notch_bank_saturated(reported_channel)
                    continue
                notch_id = bank.add_notch(candidate.frequency_hz)
                self.diagnostics.log_state_change(
                    "notch_placed",
                    after={
                        "channel": reported_channel,
                        "notch_id": notch_id,
                        "frequency_hz": candidate.frequency_hz,
                        "peak_to_average_db": candidate.peak_to_average_db,
                    },
                )

            if channel_state is not None and channel_state.mode == "live":
                # Ring-out mode never releases -- CLAUDE.md's "locks
                # filters" -- so this only runs in live mode.
                for released_id in bank.release_stale_notches(NOTCH_RELEASE_AFTER_SEC, now=now):
                    self.diagnostics.log_state_change(
                        "notch_released", after={"channel": reported_channel, "notch_id": released_id}
                    )

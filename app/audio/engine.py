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

import numpy as np

try:
    import sounddevice as sd
    _IMPORT_ERROR: OSError | None = None
except OSError as exc:
    sd = None
    _IMPORT_ERROR = exc

from app.audio.detection import FeedbackDetector
from app.audio.echo_cancellation import EchoCanceller
from app.audio.filters import NotchFilterBank
from app.config import AppConfig
from app.diagnostics.logger import DiagnosticsLogger
from app.state import AppState

ANALYSIS_QUEUE_SIZE = 64
ANALYSIS_POLL_TIMEOUT_SEC = 0.5


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

        self._stream = None
        self._analysis_queue: queue.Queue = queue.Queue(maxsize=ANALYSIS_QUEUE_SIZE)
        self._analysis_thread: threading.Thread | None = None
        self._running = threading.Event()

    def start(self) -> None:
        if sd is None:
            raise AudioEngineError("PortAudio is not available on this system") from _IMPORT_ERROR
        if self.config.audio_input_device is None or self.config.audio_output_device is None:
            raise AudioEngineError(
                "audio_input_device/audio_output_device not configured -- "
                "see app.audio.devices.list_input_devices()/list_output_devices()"
            )

        self._running.set()
        self._analysis_thread = threading.Thread(target=self._analysis_loop, name="audio-analysis", daemon=True)
        self._analysis_thread.start()

        num_channels = max(
            [*self.filter_banks.keys(), *(self.config.echo_reference_card_channels or ())],
            default=1,
        )
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

    def stop(self) -> None:
        self._running.clear()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._analysis_thread is not None:
            self._analysis_thread.join(timeout=ANALYSIS_POLL_TIMEOUT_SEC + 1)
            self._analysis_thread = None
        self.diagnostics.log_state_change("audio_engine_stopped")

    def measure_round_trip_latency(self) -> float:
        """Loopback click test per CLAUDE.md's Phase 1 plumbing step --
        needs a physical loopback cable from an output to an input on the
        configured device and must run on the target PC; not something
        this test suite can exercise."""
        raise NotImplementedError(
            "requires a physical loopback cable and the configured audio device -- run on the target PC"
        )

    def _audio_callback(self, indata: np.ndarray, outdata: np.ndarray, frames: int, time_info, status) -> None:
        """PortAudio callback. Real-time constraint: filter processing
        only, no analysis/ML, no diagnostics logging on the hot path."""
        reference_block = self._read_reference_block(indata)
        for channel_index in range(indata.shape[1]):
            channel_number = channel_index + 1
            signal = indata[:, channel_index]

            canceller = self.echo_cancellers.get(channel_number)
            if canceller is not None and reference_block is not None:
                signal = canceller.process(signal, reference_block)

            bank = self.filter_banks.get(channel_number)
            outdata[:, channel_index] = bank.process(signal) if bank is not None else signal

        try:
            self._analysis_queue.put_nowait(indata.copy())
        except queue.Full:
            pass  # analysis thread is behind -- drop this block rather than block the callback

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

    def _analyze_block(self, block: np.ndarray) -> None:
        for channel_index in range(block.shape[1]):
            channel_number = channel_index + 1
            bank = self.filter_banks.get(channel_number)
            if bank is None:
                continue
            if self.state is not None and not self.state.channels[channel_number].ai_enabled:
                continue
            for candidate in self.detector.analyze(block[:, channel_index]):
                if len(bank.active_notches()) >= bank.max_notches:
                    continue
                notch_id = bank.add_notch(candidate.frequency_hz)
                self.diagnostics.log_state_change(
                    "notch_placed",
                    after={
                        "channel": channel_number,
                        "notch_id": notch_id,
                        "frequency_hz": candidate.frequency_hz,
                        "peak_to_average_db": candidate.peak_to_average_db,
                    },
                )

"""Real-time audio engine -- NOT IMPLEMENTED THIS PHASE.

Per CLAUDE.md's "Audio engine" / build phase 1 ("Plumbing"): sounddevice
(PortAudio) with the Behringer X-USB ASIO driver, 48 kHz, 64-128 sample
buffer, target round trip <=~10 ms. The audio callback itself does ONLY
app.audio.filters.NotchFilterBank.process() -- FFT/ML analysis runs on a
separate thread (app.audio.detection, app.audio.ml.classifier) that
instructs the filter bank asynchronously. The AI is never in the audio
callback.
"""
from __future__ import annotations

from app.audio.filters import NotchFilterBank
from app.config import AppConfig
from app.diagnostics.logger import DiagnosticsLogger


class AudioEngine:
    def __init__(
        self,
        config: AppConfig,
        diagnostics: DiagnosticsLogger,
        filter_bank: NotchFilterBank | None = None,
    ) -> None:
        raise NotImplementedError("audio engine is implemented in a later phase")

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def measure_round_trip_latency(self) -> float:
        """Loopback click test per CLAUDE.md's Phase 1 plumbing step."""
        raise NotImplementedError

    def _audio_callback(self, indata, outdata, frames, time_info, status) -> None:  # noqa: ANN001
        """PortAudio callback. Real-time constraint: filter processing
        only, no allocation, no analysis/ML, no logging on the hot path
        beyond what's pre-buffered for the analysis thread to drain."""
        raise NotImplementedError

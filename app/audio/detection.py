"""Feedback candidate detection (FFT heuristics).

Per CLAUDE.md's "Audio engine" section: analysis thread consumes a ring
buffer and runs FFT peak-detection heuristics (peak-to-average ratio,
absence of harmonic structure, sustained growth) to flag candidate
frequencies. With no ML classifier yet (Phase 4 needs real ring-out
recordings first), a heuristic-confirmed candidate goes straight to
app.audio.filters.NotchFilterBank -- CLAUDE.md explicitly calls heuristics
+ notch filter bank "usable product on its own."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Hashable

import numpy as np

# Maps ChannelState.sensitivity (0-1, higher = more sensitive) to a
# peak-to-average threshold in dB (lower = easier to trigger). 0.5 (the
# field's default) maps to 12dB, this module's own original fixed default,
# so existing behavior is unchanged for anyone who never touches the slider.
MIN_THRESHOLD_DB = 4.0
MAX_THRESHOLD_DB = 20.0


def sensitivity_to_threshold_db(sensitivity: float) -> float:
    sensitivity = max(0.0, min(1.0, sensitivity))
    return MAX_THRESHOLD_DB - sensitivity * (MAX_THRESHOLD_DB - MIN_THRESHOLD_DB)


@dataclass
class FeedbackCandidate:
    frequency_hz: float
    peak_to_average_db: float
    harmonic_structure_present: bool
    sustained_growth: bool
    magnitude_history: list = field(default_factory=list)


class FeedbackDetector:
    def __init__(
        self,
        sample_rate: int,
        fft_size: int = 4096,
        peak_to_average_threshold_db: float = 12.0,
        sustained_growth_frames: int = 3,
        history_len: int = 8,
    ) -> None:
        self.sample_rate = sample_rate
        self.fft_size = fft_size
        self.peak_to_average_threshold_db = peak_to_average_threshold_db
        self.sustained_growth_frames = sustained_growth_frames
        self.history_len = history_len
        self._window = np.hanning(fft_size)
        # Keyed by (channel_key, bin_index), not bin_index alone -- a single
        # shared FeedbackDetector serving every channel (AudioEngine reuses
        # one instance across all 32 Card slots) would otherwise leak one
        # channel's sustained-growth history into another's, since the same
        # bin index means completely different signals on different
        # channels. channel_key defaults to None for standalone/single-
        # channel callers (e.g. direct unit tests), which behaves exactly
        # as before this fix.
        self._history: dict[tuple[Hashable, int], list] = {}

    def analyze(
        self,
        block: np.ndarray,
        channel_key: Hashable = None,
        threshold_db: float | None = None,
    ) -> list[FeedbackCandidate]:
        """Returns confirmed candidates for this frame. Analysis-thread
        only -- never called from the audio callback. threshold_db
        overrides self.peak_to_average_threshold_db for this call only
        (AudioEngine derives it from the channel's sensitivity/mode)."""
        threshold = threshold_db if threshold_db is not None else self.peak_to_average_threshold_db
        block = self._fit_to_fft_size(block)
        spectrum = np.abs(np.fft.rfft(block * self._window))
        freqs = np.fft.rfftfreq(self.fft_size, d=1.0 / self.sample_rate)
        avg_magnitude = float(np.mean(spectrum)) + 1e-12

        strong_peaks = []
        for idx in self._find_local_peaks(spectrum):
            magnitude = spectrum[idx]
            peak_to_average_db = 20 * np.log10(magnitude / avg_magnitude)
            if peak_to_average_db >= threshold:
                strong_peaks.append(idx)

        overtone_bins = self._find_overtone_bins(freqs, strong_peaks)

        candidates: list[FeedbackCandidate] = []
        seen_keys: set[tuple[Hashable, int]] = set()

        for idx in strong_peaks:
            magnitude = spectrum[idx]
            peak_to_average_db = 20 * np.log10(magnitude / avg_magnitude)

            key = (channel_key, idx)
            seen_keys.add(key)
            self._update_history(key, float(magnitude))
            sustained = self._is_sustained_growth(key)
            harmonic = idx in overtone_bins or self._has_harmonic_structure(spectrum, freqs, idx, avg_magnitude)

            if sustained and not harmonic:
                candidates.append(
                    FeedbackCandidate(
                        frequency_hz=float(freqs[idx]),
                        peak_to_average_db=float(peak_to_average_db),
                        harmonic_structure_present=harmonic,
                        sustained_growth=sustained,
                        magnitude_history=list(self._history[key]),
                    )
                )

        self._decay_unseen_history(channel_key, seen_keys)
        return candidates

    def _fit_to_fft_size(self, block: np.ndarray) -> np.ndarray:
        if len(block) < self.fft_size:
            return np.pad(block, (0, self.fft_size - len(block)))
        return block[-self.fft_size:]

    @staticmethod
    def _find_local_peaks(spectrum: np.ndarray) -> list[int]:
        # Isolated local-maxima peak finder -- good enough for narrowband
        # feedback tones, which show up as sharp spikes well above the
        # noise floor (unlike broadband musical content).
        return [i for i in range(1, len(spectrum) - 1) if spectrum[i] > spectrum[i - 1] and spectrum[i] > spectrum[i + 1]]

    def _update_history(self, key: tuple, magnitude: float) -> None:
        history = self._history.setdefault(key, [])
        history.append(magnitude)
        if len(history) > self.history_len:
            history.pop(0)

    def _decay_unseen_history(self, channel_key: Hashable, seen_keys: set[tuple]) -> None:
        # Only decays this channel's own keys -- otherwise a call for one
        # channel would age out every other channel's history too, since
        # they all share this one dict.
        for key in [k for k in self._history if k[0] == channel_key]:
            if key in seen_keys:
                continue
            history = self._history[key]
            history.append(0.0)
            if len(history) > self.history_len:
                history.pop(0)
            if not any(history):
                del self._history[key]

    def _is_sustained_growth(self, key: tuple) -> bool:
        """Strict increase, not merely non-decreasing -- a held constant
        level (CLAUDE.md's "sustained notes" hard negative case) must NOT
        qualify, only an escalating feedback loop should."""
        history = self._history.get(key, [])
        if len(history) < self.sustained_growth_frames:
            return False
        recent = history[-self.sustained_growth_frames:]
        return all(b > a for a, b in zip(recent, recent[1:]))

    @staticmethod
    def _find_overtone_bins(freqs: np.ndarray, strong_peaks: list[int]) -> set[int]:
        """Bins that land on the 2nd/3rd/4th harmonic of some lower strong
        peak. _has_harmonic_structure() only looks *upward* from a given
        peak, so an overtone bin itself (whose own upward harmonics are
        empty) would otherwise be flagged as an independent fundamental --
        this catches that case so a musical note's overtones aren't each
        treated as separate feedback candidates."""
        if len(strong_peaks) < 2 or len(freqs) < 2:
            return set()
        bin_width = freqs[1] - freqs[0]
        covered: set[int] = set()
        for idx in sorted(strong_peaks):
            fundamental_freq = freqs[idx]
            if fundamental_freq <= 0:
                continue
            for n in (2, 3, 4):
                harmonic_freq = fundamental_freq * n
                if harmonic_freq >= freqs[-1]:
                    break
                predicted_idx = harmonic_freq / bin_width
                for peak_idx in strong_peaks:
                    if peak_idx != idx and abs(peak_idx - predicted_idx) <= 1:
                        covered.add(peak_idx)
        return covered

    def _has_harmonic_structure(self, spectrum: np.ndarray, freqs: np.ndarray, idx: int, avg_magnitude: float) -> bool:
        """Absence of harmonic structure is a feedback indicator (a pure
        squeal is usually one tone, not a note with overtones); strong
        harmonics suggest musical content instead."""
        fundamental_freq = freqs[idx]
        if fundamental_freq <= 0:
            return False
        bin_width = freqs[1] - freqs[0]
        harmonic_hits = 0
        for n in (2, 3, 4):
            harmonic_freq = fundamental_freq * n
            if harmonic_freq >= freqs[-1]:
                break
            harmonic_idx = int(round(harmonic_freq / bin_width))
            if harmonic_idx < len(spectrum) and spectrum[harmonic_idx] > avg_magnitude * 4:
                harmonic_hits += 1
        return harmonic_hits >= 2

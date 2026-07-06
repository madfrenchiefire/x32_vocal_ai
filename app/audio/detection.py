"""Feedback candidate detection (FFT heuristics) -- NOT IMPLEMENTED THIS PHASE.

Per CLAUDE.md's "Audio engine" section: analysis thread consumes a ring
buffer and runs FFT peak-detection heuristics (peak-to-average ratio,
absence of harmonic structure, sustained growth) to flag candidate
frequencies. Confirmed/vetoed by app.audio.ml.classifier before
app.audio.filters.NotchFilterBank places a notch.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy


class FeedbackDetector:
    def __init__(self, sample_rate: int) -> None:
        raise NotImplementedError("feedback detection heuristics are implemented in a later phase")

    def analyze(self, block: numpy.ndarray) -> list[dict]:
        """Returns candidate {frequency_hz, peak_to_average_ratio,
        sustained_growth, harmonic_structure} dicts for the ML classifier
        to confirm or veto."""
        raise NotImplementedError
